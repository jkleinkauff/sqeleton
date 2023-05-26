from dataclasses import field
from datetime import datetime
from typing import Any, Generator, List, Optional, Sequence, Union, Dict, Literal
from collections import ChainMap
from functools import lru_cache

from runtype import dataclass as _dataclass, cv_type_checking

from ..utils import join_iter, ArithString
from ..abcs import AbstractCompiler, Compilable
from ..abcs.database_types import AbstractTable, AbstractDialect
from ..abcs.mixins import AbstractMixin_Regex, AbstractMixin_TimeTravel
from ..schema import Schema

from .compiler import Compiler, cv_params, Root, CompileError
from .base import SKIP, DbPath, args_as_tuple, SqeletonError


class QueryBuilderError(SqeletonError):
    pass


class QB_TypeError(QueryBuilderError):
    pass


dataclass = _dataclass(eq=False, order=False)

ellipsis = type(Ellipsis)


def cache(user_function, /):
    'Simple lightweight unbounded cache.  Sometimes called "memoize".'
    # Taken from https://github.com/python/cpython/blob/3.11/Lib/functools.py
    return lru_cache(maxsize=None)(user_function)



class CompilableNode(Compilable):
    "Base class for query expression nodes"

    type: Any = None

    def _dfs_values(self):
        yield self
        for k, vs in dict(self).items():  # __dict__ provided by runtype.dataclass
            if k == "source_table":
                # Skip data-sources, we're only interested in data-parameters
                continue
            if not isinstance(vs, (list, tuple)):
                vs = [vs]
            for v in vs:
                if isinstance(v, ExprNode):
                    yield from v._dfs_values()


class ExprNode(CompilableNode):
    "Base class for query expression nodes"

    def cast_to(self, to):
        return Cast(self, to)


# Query expressions can only interact with objects that are an instance of 'Expr'
Expr = Union[ExprNode, str, bool, int, float, datetime, ArithString, None]


@dataclass
class Code(ExprNode, Root):
    code: str
    args: Dict[str, Expr] = None

    def compile(self, c: Compiler) -> str:
        if not self.args:
            return self.code

        args = {k: c.compile(v) for k, v in self.args.items()}
        return self.code.format(**args)


def _expr_type(e: Expr) -> type:
    if isinstance(e, ExprNode):
        return e.type
    return type(e)


@dataclass
class Alias(ExprNode):
    expr: Expr
    name: str

    def compile(self, c: Compiler) -> str:
        return f"{c.compile(self.expr)} AS {c.quote(self.name)}"

    @property
    def type(self):
        return _expr_type(self.expr)


def _drop_skips(exprs):
    return [e for e in exprs if e is not SKIP]


def _drop_skips_dict(exprs_dict):
    return {k: v for k, v in exprs_dict.items() if v is not SKIP}


class ITable(AbstractTable):
    source_table: Any
    schema: Schema = None

    def select(self, *exprs, distinct=SKIP, optimizer_hints=SKIP, **named_exprs) -> "Select":
        """Create a new table with the specified fields"""
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)
        resolve_names(self.source_table, exprs)
        return Select.make(self, columns=exprs, distinct=distinct, optimizer_hints=optimizer_hints)

    def where(self, *exprs) -> "Select":
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.source_table, exprs)
        return Select.make(self, where_exprs=exprs)

    def order_by(self, *exprs):
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.source_table, exprs)
        return Select.make(self, order_by_exprs=exprs)

    def limit(self, limit: int):
        if limit is SKIP:
            return self

        return Select.make(self, limit_expr=limit)

    def join(self, target: "ITable"):
        """Join this table with the target table."""
        return Join([self, target])

    def group_by(self, *keys) -> "GroupBy":
        """Group according to the given keys.

        Must be followed by a call to :ref:``GroupBy.agg()``
        """
        keys = _drop_skips(keys)
        resolve_names(self.source_table, keys)

        return GroupBy(self, keys)

    def _get_column(self, name: str):
        if self.schema:
            name = self.schema.get_key(name)  # Get the actual name. Might be case-insensitive.
        return Column(self, name)

    # def __getattr__(self, column):
    #     return self._get_column(column)

    def __getitem__(self, column):
        if not isinstance(column, str):
            raise TypeError()
        return self._get_column(column)

    def count(self):
        return Select(self, [Count()])

    def union(self, other: "ITable"):
        """SELECT * FROM self UNION other"""
        return TableOp("UNION", self, other)

    def union_all(self, other: "ITable"):
        """SELECT * FROM self UNION ALL other"""
        return TableOp("UNION ALL", self, other)

    def minus(self, other: "ITable"):
        """SELECT * FROM self EXCEPT other"""
        # aka
        return TableOp("EXCEPT", self, other)

    def intersect(self, other: "ITable"):
        """SELECT * FROM self INTERSECT other"""
        return TableOp("INTERSECT", self, other)

    def alias(self, name):
        if isinstance(self, TableAlias):
            return self.replace(name=name)
        return TableAlias(self, name)


@dataclass
class Concat(ExprNode):
    exprs: list
    sep: str = None

    def compile(self, c: Compiler) -> str:
        # We coalesce because on some DBs (e.g. MySQL) concat('a', NULL) is NULL
        items = [f"coalesce({c.compile(Code(c.dialect.to_string(c.compile(expr))))}, '<null>')" for expr in self.exprs]
        assert items
        if len(items) == 1:
            return items[0]

        if self.sep:
            items = list(join_iter(f"'{self.sep}'", items))
        return c.dialect.concat(items)


@dataclass
class Count(ExprNode):
    expr: Expr = None
    distinct: bool = False

    type = int

    def compile(self, c: Compiler) -> str:
        expr = c.compile(self.expr) if self.expr else "*"
        if self.distinct:
            return f"count(distinct {expr})"

        return f"count({expr})"


class LazyOps:
    def __add__(self, other):
        return BinOp("+", [self, other])

    def __sub__(self, other):
        return BinOp("-", [self, other])

    def __mul__(self, other):
        return BinOp("*", [self, other])

    def __div__(self, other):
        return BinOp("/", [self, other])

    def __neg__(self):
        return UnaryOp("-", self)

    def __gt__(self, other):
        return BinBoolOp(">", [self, other])

    def __ge__(self, other):
        return BinBoolOp(">=", [self, other])

    def __eq__(self, other):
        if cv_type_checking.get():
            return super().__eq__(other)

        if other is None:
            return BinBoolOp("IS", [self, None])

        return BinBoolOp("=", [self, other])

    def __lt__(self, other):
        return BinBoolOp("<", [self, other])

    def __le__(self, other):
        return BinBoolOp("<=", [self, other])

    def __or__(self, other):
        return BinBoolOp("OR", [self, other])

    def __and__(self, other):
        return BinBoolOp("AND", [self, other])

    def is_distinct_from(self, other):
        return IsDistinctFrom(self, other)

    def like(self, other):
        return BinBoolOp("LIKE", [self, other])

    def in_(self, *others):
        others = args_as_tuple(others)
        assert isinstance(others, tuple), f"Only lists of constants are supported for now, not {others}"
        if len(others) == 0:
            return False  # SQL value
        elif len(others) == 1 and isinstance(others[0], ExprTable):
            return InTable(self, others[0])
        return In(self, others)

    def test_regex(self, other):
        return TestRegex(self, other)

    def sum(self):
        return Func("SUM", [self])

    def count(self):
        return Func("COUNT", [self])

    def max(self):
        return Func("MAX", [self])

    def min(self):
        return Func("MIN", [self])


@dataclass
class TestRegex(ExprNode, LazyOps):
    string: Expr
    pattern: Expr

    def compile(self, c: Compiler) -> str:
        if not isinstance(c.dialect, AbstractMixin_Regex):
            raise NotImplementedError(f"No regex implementation for database '{c.database}'")
        regex = c.dialect.test_regex(self.string, self.pattern)
        return c.compile(regex)


@dataclass
class Func(ExprNode, LazyOps):
    name: str
    args: Sequence[Expr]

    def compile(self, c: Compiler) -> str:
        args = ", ".join(c.compile(e) for e in self.args)
        return f"{self.name}({args})"


@dataclass
class WhenThen(ExprNode):
    when: Expr
    then: Expr

    def compile(self, c: Compiler) -> str:
        return f"WHEN {c.compile(self.when)} THEN {c.compile(self.then)}"


@dataclass
class CaseWhen(ExprNode, LazyOps):
    cases: Sequence[WhenThen]
    else_expr: Expr = None

    def compile(self, c: Compiler) -> str:
        assert self.cases
        when_thens = " ".join(c.compile(case) for case in self.cases)
        else_expr = (" ELSE " + c.compile(self.else_expr)) if self.else_expr is not None else ""
        return f"CASE {when_thens}{else_expr} END"

    @property
    def type(self):
        then_types = {_expr_type(case.then) for case in self.cases}
        if self.else_expr:
            then_types.add(_expr_type(self.else_expr))
        if len(then_types) > 1:
            raise QB_TypeError(f"Non-matching types in when: {then_types}")
        (t,) = then_types
        return t

    def when(self, *whens: Expr) -> "QB_When":
        """Add a new 'when' clause to the case expression

        Must be followed by a call to `.then()`
        """
        whens = args_as_tuple(whens)
        whens = _drop_skips(whens)
        if not whens:
            raise QueryBuilderError("Expected valid whens")

        # XXX reimplementing api.and_()
        if len(whens) == 1:
            return QB_When(self, whens[0])
        return QB_When(self, BinBoolOp("AND", whens))

    def else_(self, then: Expr):
        """Add an 'else' clause to the case expression.

        Can only be called once!
        """
        if self.else_expr is not None:
            raise QueryBuilderError(f"Else clause already specified in {self}")

        return self.replace(else_expr=then)


@dataclass
class QB_When:
    "Partial case-when, used for query-building"
    casewhen: CaseWhen
    when: Expr

    def then(self, then: Expr) -> CaseWhen:
        """Add a 'then' clause after a 'when' was added."""
        case = WhenThen(self.when, then)
        return self.casewhen.replace(cases=self.casewhen.cases + [case])


@_dataclass(eq=False, order=False)
class IsDistinctFrom(ExprNode, LazyOps):
    a: Expr
    b: Expr
    type = bool

    def compile(self, c: Compiler) -> str:
        return c.dialect.is_distinct_from(c.compile(self.a), c.compile(self.b))


@_dataclass(eq=False, order=False)
class BinOp(ExprNode, LazyOps):
    op: str
    args: Sequence[Expr]

    def compile(self, c: Compiler) -> str:
        expr = f" {self.op} ".join(c.compile(a) for a in self.args)
        return f"({expr})"

    @property
    def type(self):
        types = {_expr_type(i) for i in self.args}
        if len(types) > 1:
            raise TypeError(f"Expected all args to have the same type, got {types}")
        (t,) = types
        return t


@dataclass
class UnaryOp(ExprNode, LazyOps):
    op: str
    expr: Expr

    def compile(self, c: Compiler) -> str:
        return f"({self.op}{c.compile(self.expr)})"

    @property
    def type(self):
        return self.expr.type


class BinBoolOp(BinOp):
    type = bool


@_dataclass(eq=False, order=False)
class Column(ExprNode, LazyOps):
    source_table: ITable
    name: str

    @property
    def type(self):
        if self.source_table.schema is None:
            raise QueryBuilderError(f"Schema required for table {self.source_table}")
        return self.source_table.schema[self.name]

    def compile(self, c: Compiler) -> str:
        if c._table_context:
            if len(c._table_context) > 1:
                aliases = [
                    t for t in c._table_context if isinstance(t, TableAlias)
                    and (t.source_table is self.source_table or t is self.source_table)
                ]
                if not aliases:
                    return c.quote(self.name)
                elif len(aliases) > 1:
                    names = [a.name for a in aliases]
                    raise CompileError(f"Too many aliases for column {self.name} between tables: {names}")
                (alias,) = aliases

                return f"{c.quote(alias.name)}.{c.quote(self.name)}"

        return c.quote(self.name)




class ExprTable(ExprNode, ITable):
    pass


@_dataclass
class TablePath(ExprTable):
    path: DbPath
    schema: Optional[Schema] = field(default=None, repr=False)

    @property
    def source_table(self):
        return self

    def to_string(self, dialect: AbstractDialect):
        return ".".join(map(dialect.quote, self.path))

    def compile(self, c: Compiler) -> str:
        # path = self.path  # c.database._normalize_table_path(self.name)
        # return ".".join(map(c.quote, path))
        return self.to_string(c.dialect)

    def __repr__(self) -> str:
        if self.schema:
            return f"TablePath({self.path!r}, schema=<{len(self.schema)} cols>)"
        return f"TablePath({self.path!r})"

    # Statement shorthands
    def create(self, source_table: ITable = None, *, if_not_exists: bool = False, primary_keys: List[str] = None):
        """Returns a query expression to create a new table.

        Parameters:
            source_table: a table expression to use for initializing the table.
                          If not provided, the table must have a schema specified.
            if_not_exists: Add a 'if not exists' clause or not. (note: not all dbs support it!)
            primary_keys: List of column names which define the primary key
        """

        if source_table is None and not self.schema:
            raise ValueError("Either schema or source table needed to create table")
        if isinstance(source_table, TablePath):
            source_table = source_table.select()
        return CreateTable(self, source_table, if_not_exists=if_not_exists, primary_keys=primary_keys)

    def drop(self, if_exists=False):
        """Returns a query expression to delete the table.

        Parameters:
            if_not_exists: Add a 'if not exists' clause or not. (note: not all dbs support it!)
        """
        return DropTable(self, if_exists=if_exists)

    def truncate(self):
        """Returns a query expression to truncate the table. (remove all rows)"""
        return TruncateTable(self)

    def delete_rows(self, *where_exprs: Expr):
        where_exprs = args_as_tuple(where_exprs)
        where_exprs = _drop_skips(where_exprs)
        if not where_exprs:
            return self

        resolve_names(self.source_table, where_exprs)
        return DeleteFromTable(self, where_exprs)

    def update_fields(self, *where_exprs: Expr, **kv):
        where_exprs = args_as_tuple(where_exprs)
        where_exprs = _drop_skips(where_exprs)
        resolve_names(self.source_table, where_exprs)
        resolve_names(self.source_table, kv.values())
        return UpdateTable(self, kv, where_exprs)

    def insert_rows(self, rows: Sequence, *, columns: List[str] = None):
        """Returns a query expression to insert rows to the table, given as Python values.

        Parameters:
            rows: A list of tuples. Must all have the same width.
            columns: Names of columns being populated. If specified, must have the same length as the tuples.
        """
        # TODO support expressions (now, random, etc.)
        rows = list(rows)
        if not rows:
            return SKIP

        if isinstance(rows[0], dict):
            keys = list(rows[0].keys())
            if not columns:
                columns = keys
            elif not (set(columns) <= set(rows[0].keys())):
                raise ValueError("Keys in dictionary are not a subset of 'columns'")
            rows = [[row[k] for k in columns] for row in rows]

        return InsertToTable(self, ConstantTable(rows), columns=columns)

    def insert_row(self, *values, columns: List[str] = None, **kw):
        """Returns a query expression to insert a single row to the table, given as Python values.

        Parameters:
            columns: Names of columns being populated. If specified, must have the same length as 'values'
        """
        if (not values) == (not kw):
            raise ValueError("Must provide either positional arguments or keyword arguments, but not a mix of both.")
        if values:
            return InsertToTable(self, ConstantTable([values]), columns=columns)

        assert kw
        assert not columns
        return InsertToTable(self, ConstantTable([list(kw.values())]), columns=list(kw.keys()))

    def insert_expr(self, expr: Expr):
        """Returns a query expression to insert rows to the table, given as a query expression.

        Parameters:
            expr: query expression to from which to read the rows
        """
        if isinstance(expr, TablePath):
            expr = expr.select()
        return InsertToTable(self, expr)

    def time_travel(
        self, *, before: bool = False, timestamp: datetime = None, offset: int = None, statement: str = None
    ) -> Compilable:
        """Selects historical data from the table

        Parameters:
            before: If false, inclusive of the specified point in time.
                     If True, only return the time before it. (at/before)
            timestamp: A constant timestamp
            offset: the time 'offset' seconds before now
            statement: identifier for statement, e.g. query ID

        Must specify exactly one of `timestamp`, `offset` or `statement`.
        """
        if sum(int(i is not None) for i in (timestamp, offset, statement)) != 1:
            raise ValueError("Must specify exactly one of `timestamp`, `offset` or `statement`.")

        if timestamp is not None:
            assert offset is None and statement is None


@_dataclass
class ForeignKey:
    table: TablePath
    field: str


@dataclass
class TableAlias(ExprNode, ITable):
    source_table: ITable
    name: str

    def compile(self, c: Compiler) -> str:
        return f"{c.compile(self.source_table)} {c.quote(self.name)}"

    @property
    def schema(self):
        return self.source_table.schema



ColumnsDef = Sequence[Union[Expr, ellipsis]]
def _expand_ellipsis(schema: dict, columns: ColumnsDef):
    for c in columns:
        if c is ...:
            # select all, i.e. *
            yield from schema.items()
        else:
            yield c.name, c.type

def _union_dicts(*ds):
    unioned = {}
    for d in ds:
        unioned.update(d)
    return unioned

@dataclass
class Join(ExprNode, ITable, Root):
    source_tables: Sequence[ITable]
    op: str = None
    on_exprs: Sequence[Expr] = None
    columns: ColumnsDef = None

    @property
    def source_table(self):
        return self

    @property
    @cache
    def schema(self):
        if not self.columns:
            schemas = [t.schema for t in self.source_tables if t.schema]
            assert schemas and all(schemas)
            return type(schemas[0])(ChainMap(*schemas))  # TODO merge dictionaries in compliance with SQL dialect!

        # TODO validate types match between both tables
        schemas = [s.schema for s in self.source_tables]
        d = _union_dicts(*schemas)
        return type(schemas[0])(dict(_expand_ellipsis(d, self.columns)))

    def on(self, *exprs) -> "Join":
        """Add an ON clause, for filtering the result of the cartesian product (i.e. the JOIN)"""
        if len(exprs) == 1:
            (e,) = exprs
            if isinstance(e, Generator):
                exprs = tuple(e)

        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        return self.replace(on_exprs=(self.on_exprs or []) + exprs)

    def select(self, *exprs, **named_exprs) -> "Join":
        """Select fields to return from the JOIN operation

        See Also: ``ITable.select()``
        """
        if self.columns is not None:
            # join-select already applied
            return super().select(*exprs, **named_exprs)

        exprs = _drop_skips(exprs)
        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)
        resolve_names(self.source_table, exprs)
        # TODO Ensure exprs <= self.columns ?
        return self.replace(columns=exprs)

    def compile(self, parent_c: Compiler) -> str:
        tables = [
            t if isinstance(t, TableAlias) else TableAlias(t, parent_c.new_unique_name()) for t in self.source_tables
        ]
        c = parent_c.replace(in_select=True)
        op = " JOIN " if self.op is None else f" {self.op} JOIN "
        joined = op.join(c.compile(t) for t in tables)
        c = parent_c.add_table_context(*tables, in_select=True)

        if self.on_exprs:
            on = " AND ".join(c.compile(e) for e in self.on_exprs)
            res = f"{joined} ON {on}"
        else:
            res = joined

        columns = "*" if not self.columns else ", ".join(map(c.compile, self.columns))
        select = f"SELECT {columns} FROM {res}"

        # TODO work with TableAlias
        if parent_c.in_select:
            select = f"({select})"
        return select


@dataclass
class GroupBy(ExprNode, ITable, Root):
    table: ITable
    keys: Sequence[Expr] = None  # IKey?
    values: Sequence[Expr] = None
    having_exprs: Sequence[Expr] = None

    @property
    def source_table(self):
        return self

    @property
    @cache
    def schema(self):
        s = self.table.schema
        if s is None:
            return None
        return type(s)({c.name: c.type for c in self.keys + self.values})

    def __post_init__(self):
        assert self.keys or self.values

    def having(self, *exprs):
        """Add a 'HAVING' clause to the group-by"""
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.table, exprs)
        return self.replace(having_exprs=(self.having_exprs or []) + exprs)

    def agg(self, *exprs, **named_exprs):
        """Select aggregated fields for the group-by."""
        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)

        named_exprs = _drop_skips_dict(named_exprs)
        exprs += _named_exprs_as_aliases(named_exprs)

        resolve_names(self.table, exprs)
        return self.replace(values=(self.values or []) + exprs)

    def compile(self, c: Compiler) -> str:
        if self.values is None:
            raise CompileError(".group_by() must be followed by a call to .agg()")

        keys = [str(i + 1) for i in range(len(self.keys))]
        columns = (self.keys or []) + (self.values or [])
        if isinstance(self.table, Select) and not self.table.columns and self.table.group_by_exprs is None:
            return c.compile(
                self.table.replace(
                    columns=columns,
                    group_by_exprs=[Code(k) for k in keys],
                    having_exprs=self.having_exprs,
                )
            )

        keys_str = ", ".join(keys)
        columns_str = ", ".join(c.compile(x) for x in columns)
        having_str = (
            " HAVING " + " AND ".join(map(c.compile, self.having_exprs)) if self.having_exprs is not None else ""
        )
        select = (
            f"SELECT {columns_str} FROM {SelectCompiler(c).compile(self.table)} GROUP BY {keys_str}{having_str}"
        )

        if c.in_select:
            select = f"({select})"
        return select


@dataclass
class TableOp(ExprNode, ITable, Root):
    op: str
    table1: ITable
    table2: ITable

    @property
    def source_table(self):
        return self

    @property
    def type(self):
        # TODO ensure types of both tables are compatible
        return self.table1.type

    @property
    def schema(self):
        s1 = self.table1.schema
        s2 = self.table2.schema
        assert len(s1) == len(s2)
        return s1

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(in_select=False)
        table_expr = f"{c.compile(self.table1)} {self.op} {c.compile(self.table2)}"
        if parent_c.in_select:
            table_expr = f"({table_expr})"
        return table_expr


@dataclass
class SelectCompiler(AbstractCompiler):
    c: Compiler

    def compile(self, elem: Any, params: Dict[str, Any] = None) -> str:
        if isinstance(elem, (Select, TableOp, GroupBy, Join)):
            elem = TableAlias(elem, self.c.new_unique_name()) 
        c = self.c.replace(in_select=True)
        return c.compile(elem, params)

    @property
    def dialect(self):
        return self.c.dialect

@dataclass
class Select(ExprTable, Root):
    table: Expr = None
    columns: ColumnsDef = None
    where_exprs: Sequence[Expr] = None
    order_by_exprs: Sequence[Expr] = None
    group_by_exprs: Sequence[Expr] = None
    having_exprs: Sequence[Expr] = None
    limit_expr: int = None
    distinct: bool = False
    optimizer_hints: Sequence[Expr] = None

    @property
    @cache
    def schema(self):
        s = self.table.schema
        if s is None or not self.columns:
            return s
        return type(s)(dict(_expand_ellipsis(s, self.columns)))

    @property
    def source_table(self):
        return self

    def compile(self, parent_c: Compiler) -> str:
        c = SelectCompiler(parent_c)

        columns = ", ".join(map(c.compile, self.columns)) if self.columns else "*"
        distinct = "DISTINCT " if self.distinct else ""
        optimizer_hints = c.dialect.optimizer_hints(self.optimizer_hints) if self.optimizer_hints else ""
        select = f"SELECT {optimizer_hints}{distinct}{columns}"

        if self.table:
            select += " FROM " + c.compile(self.table)
        elif c.dialect.PLACEHOLDER_TABLE:
            select += f" FROM {c.dialect.PLACEHOLDER_TABLE}"

        if self.where_exprs:
            select += " WHERE " + " AND ".join(map(c.compile, self.where_exprs))

        if self.group_by_exprs:
            select += " GROUP BY " + ", ".join(map(c.compile, self.group_by_exprs))

        if self.having_exprs:
            assert self.group_by_exprs
            select += " HAVING " + " AND ".join(map(c.compile, self.having_exprs))

        if self.order_by_exprs:
            select += " ORDER BY " + ", ".join(map(c.compile, self.order_by_exprs))

        if self.limit_expr is not None:
            select += " " + c.dialect.offset_limit(0, self.limit_expr)

        if parent_c.in_select:
            select = f"({select})"
        return select

    @classmethod
    def make(cls, table: ITable, distinct: bool = SKIP, optimizer_hints: str = SKIP, **kwargs):
        assert "table" not in kwargs

        if not isinstance(table, cls):  # If not Select
            if distinct is not SKIP:
                kwargs["distinct"] = distinct
            if optimizer_hints is not SKIP:
                kwargs["optimizer_hints"] = optimizer_hints
            return cls(table, **kwargs)

        # We can safely assume isinstance(table, Select)
        if optimizer_hints is not SKIP:
            kwargs["optimizer_hints"] = optimizer_hints

        if distinct is not SKIP:
            if distinct is False and table.distinct:
                return cls(table, **kwargs)
            kwargs["distinct"] = distinct

        if table.limit_expr or table.group_by_exprs:
            return cls(table, **kwargs)

        # Fill in missing attributes, instead of nesting instances
        for k, v in kwargs.items():
            if getattr(table, k) is not None:
                if k == "where_exprs":  # Additive attribute
                    kwargs[k] = getattr(table, k) + v
                elif k in ["distinct", "optimizer_hints"]:
                    pass
                else:
                    raise ValueError(k)

        return table.replace(**kwargs)


@dataclass
class Cte(ExprNode, ITable):
    source_table: Expr
    name: str = None
    params: Sequence[str] = None

    def compile(self, parent_c: Compiler) -> str:
        c = parent_c.replace(_table_context=[], in_select=False)
        compiled = c.compile(self.source_table)

        name = self.name or parent_c.new_unique_name()
        name_params = f"{name}({', '.join(self.params)})" if self.params else name
        parent_c._subqueries[name_params] = compiled

        return name

    @property
    def schema(self):
        # TODO add cte to schema
        return self.source_table.schema


def _named_exprs_as_aliases(named_exprs):
    return [Alias(expr, name) for name, expr in named_exprs.items()]


def resolve_names(source_table, exprs):
    i = 0
    for expr in exprs:
        # Iterate recursively and update _ResolveColumn instances with the right expression
        if isinstance(expr, ExprNode):
            for v in expr._dfs_values():
                if isinstance(v, _ResolveColumn):
                    v.resolve(source_table._get_column(v.resolve_name))
                    i += 1


@_dataclass(frozen=False, eq=False, order=False)
class _ResolveColumn(ExprNode, LazyOps):
    resolve_name: str
    resolved: Expr = None

    def resolve(self, expr: Expr):
        if self.resolved is not None:
            raise QueryBuilderError(f"Column '{self.resolve_name}' Already resolved! To value: {self.resolved}")
        self.resolved = expr

    def _get_resolved(self) -> Expr:
        if self.resolved is None:
            breakpoint()
            raise QueryBuilderError(f"Column not resolved: {self.resolve_name}")
        return self.resolved

    def compile(self, c: Compiler) -> str:
        return self._get_resolved().compile(c)

    @property
    def type(self):
        return self._get_resolved().type

    @property
    def name(self):
        return self._get_resolved().name


class This:
    """Builder object for accessing table attributes.

    Automatically evaluates to the the 'top-most' table during compilation.
    """

    def __getattr__(self, name):
        return _ResolveColumn(name)

    def __getitem__(self, name):
        if isinstance(name, (list, tuple)):
            return [_ResolveColumn(n) for n in name]
        return _ResolveColumn(name)


@dataclass
class In(ExprNode):
    expr: Expr
    list: Sequence[Expr]

    type = bool

    def compile(self, c: Compiler):
        elems = ", ".join(map(c.compile, self.list))
        return f"({c.compile(self.expr)} IN ({elems}))"


@dataclass
class InTable(ExprNode):
    expr: Expr
    source_table: ExprTable

    type = bool

    def compile(self, c: Compiler):
        return f"({c.compile(self.expr)} IN ({self.source_table.compile(c.replace(in_select=False))}))"


@dataclass
class Cast(ExprNode):
    expr: Expr
    target_type: Expr

    def compile(self, c: Compiler) -> str:
        return f"cast({c.compile(self.expr)} as {c.compile(self.target_type)})"


@dataclass
class Random(ExprNode, LazyOps):
    type = float

    def compile(self, c: Compiler) -> str:
        return c.dialect.random()


@dataclass
class ConstantTable(ExprNode):
    rows: Sequence[Sequence]

    def compile(self, c: Compiler) -> str:
        raise NotImplementedError()

    def compile_for_insert(self, c: Compiler):
        compiled_rows = [[c.compile(v) for v in r] for r in self.rows]
        return c.dialect.immediate_values(compiled_rows)


@dataclass
class Explain(ExprNode, Root):
    select: Select

    type = str

    def compile(self, c: Compiler) -> str:
        return c.dialect.explain_as_text(c.compile(self.select))


@dataclass
class CurrentTimestamp(ExprNode):
    type = datetime

    def compile(self, c: Compiler) -> str:
        return c.dialect.current_timestamp()


@dataclass
class TimeTravel(ITable):
    table: TablePath
    before: bool = False
    timestamp: datetime = None
    offset: int = None
    statement: str = None

    def compile(self, c: Compiler) -> str:
        assert isinstance(c, AbstractMixin_TimeTravel)
        return c.compile(
            c.time_travel(
                self.table, before=self.before, timestamp=self.timestamp, offset=self.offset, statement=self.statement
            )
        )


# DDL


class Statement(CompilableNode, Root):
    type = None


@dataclass
class CreateTable(Statement):
    path: TablePath
    source_table: Expr = None
    if_not_exists: bool = False
    primary_keys: List[str] = None

    def compile(self, c: Compiler) -> str:
        ne = "IF NOT EXISTS " if self.if_not_exists else ""
        if self.source_table:
            return f"CREATE TABLE {ne}{c.compile(self.path)} AS {c.compile(self.source_table)}"

        schema = ", ".join(f"{c.dialect.quote(k)} {c.dialect.type_repr(v)}" for k, v in self.path.schema.items())
        pks = (
            ", PRIMARY KEY (%s)" % ", ".join(self.primary_keys)
            if self.primary_keys and c.dialect.SUPPORTS_PRIMARY_KEY
            else ""
        )
        return f"CREATE TABLE {ne}{c.compile(self.path)}({schema}{pks})"


@dataclass
class DropTable(Statement):
    path: TablePath
    if_exists: bool = False

    def compile(self, c: Compiler) -> str:
        ie = "IF EXISTS " if self.if_exists else ""
        return f"DROP TABLE {ie}{c.compile(self.path)}"


@dataclass
class TruncateTable(Statement):
    path: TablePath

    def compile(self, c: Compiler) -> str:
        return f"TRUNCATE TABLE {c.compile(self.path)}"


@dataclass
class DeleteFromTable(Statement):
    path: TablePath
    where_exprs: Sequence[Expr] = None

    def compile(self, c: Compiler) -> str:
        delete = f"DELETE FROM {c.compile(self.path)}"
        if self.where_exprs:
            delete += " WHERE " + " AND ".join(map(c.compile, self.where_exprs))
        return delete


@dataclass
class UpdateTable(Statement):
    path: TablePath
    updates: Dict[str, Expr]
    where_exprs: Sequence[Expr] = None

    def compile(self, c: Compiler) -> str:
        updates = [f"{k} = {c.compile(v)}" for k, v in self.updates.items()]
        update = f"UPDATE {c.compile(self.path)} SET " + ", ".join(updates)

        if self.where_exprs:
            update += " WHERE " + " AND ".join(map(c.compile, self.where_exprs))
        return update


@dataclass
class InsertToTable(Statement):
    path: TablePath
    expr: Expr
    columns: List[str] = None
    returning_exprs: List[str] = None

    def compile(self, c: Compiler) -> str:
        if isinstance(self.expr, ConstantTable):
            expr = self.expr.compile_for_insert(c)
        else:
            expr = c.compile(self.expr)

        columns = "(%s)" % ", ".join(map(c.quote, self.columns)) if self.columns is not None else ""

        return f"INSERT INTO {c.compile(self.path)}{columns} {expr}"

    def returning(self, *exprs):
        """Add a 'RETURNING' clause to the insert expression.

        Note: Not all databases support this feature!
        """
        if self.returning_exprs:
            raise ValueError("A returning clause is already specified")

        exprs = args_as_tuple(exprs)
        exprs = _drop_skips(exprs)
        if not exprs:
            return self

        resolve_names(self.path, exprs)
        return self.replace(returning_exprs=exprs)


@dataclass
class Commit(Statement):
    """Generate a COMMIT statement, if we're in the middle of a transaction, or in auto-commit. Otherwise SKIP."""

    def compile(self, c: Compiler) -> str:
        return "COMMIT" if not c.database.is_autocommit else SKIP


@dataclass
class Param(ExprNode, ITable):
    """A value placeholder, to be specified at compilation time using the `cv_params` context variable."""

    name: str

    @property
    def source_table(self):
        return self

    def compile(self, c: Compiler) -> str:
        params = cv_params.get()
        return c._compile(params[self.name])
