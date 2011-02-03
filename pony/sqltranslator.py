import __builtin__, types
from compiler import ast
from types import NoneType
from operator import attrgetter
from itertools import imap, izip

from pony import orm
from pony.decompiling import decompile
from pony.templating import Html, StrHtml
from pony.dbapiprovider import SQLBuilder
from pony.sqlsymbols import *

MAX_ALIAS_LENGTH = 30

class TranslationError(Exception): pass

python_ast_cache = {}
sql_cache = {}

def select(gen):
    tree, external_names = decompile(gen)
    globals = gen.gi_frame.f_globals
    locals = gen.gi_frame.f_locals
    variables = {}
    functions = {}
    for name in external_names:
        try: value = locals[name]
        except KeyError:
            try: value = globals[name]
            except KeyError:
                try: value = getattr(__builtin__, name)
                except AttributeError: raise NameError, name
        if value in special_functions: functions[name] = value
        elif type(value) in (types.FunctionType, types.BuiltinFunctionType):
            raise TypeError('Function %r cannot be used inside query' % value.__name__)
        elif type(value) is types.MethodType:
            raise TypeError('Method %r cannot be used inside query' % value.__name__)
        else: variables[name] = value
    vartypes = dict((name, get_normalized_type(value)) for name, value in variables.iteritems())
    return Query(gen, tree, vartypes, functions, variables)

def exists(subquery):
    raise TypeError('Function exists() can be used inside query only')

class Query(object):
    def __init__(query, gen, tree, vartypes, functions, variables):
        query._gen = gen
        query._tree = tree
        query._vartypes = vartypes
        query._variables = variables
        query._result = None
        key = gen.gi_frame.f_code, tuple(sorted(vartypes.iteritems())), tuple(sorted(functions.iteritems()))
        query._python_ast_key = key
        translator = python_ast_cache.get(key)
        if translator is None:
            translator = SQLTranslator(tree, vartypes, functions)
            python_ast_cache[key] = translator
        query._translator = translator
        query._database = translator.entity._diagram_.database
        query._order = None
        query._limit = None
    def __iter__(query):
        translator = query._translator
        sql_key = query._python_ast_key + (query._order, query._limit)
        cache_entry = sql_cache.get(sql_key)
        database = query._database
        if cache_entry is None:
            sql_ast = translator.sql_ast
            if query._order:
                alias = translator.alias
                orderby_section = [ ORDER_BY ]
                for attr, asc in query._order:
                    for column in attr.columns:
                        orderby_section.append(([COLUMN, alias, column], asc and ASC or DESC))
                sql_ast = sql_ast + [ orderby_section ]
            if query._limit:
                start, stop = query._limit
                limit = stop - start
                offset = start
                assert limit is not None
                limit_section = [ LIMIT, [ VALUE, limit ]]
                if offset: limit_section.append([ VALUE, offset ])
                sql_ast = sql_ast + [ limit_section ]
            con, provider = database._get_connection()
            sql, adapter = provider.ast2sql(con, sql_ast)
            cache_entry = sql, adapter
            sql_cache[sql_key] = cache_entry
        else: sql, adapter = cache_entry
        param_dict = {}
        for param_name, extractor in translator.extractors.items():
            param_dict[param_name] = extractor(query._variables)
        arguments = adapter(param_dict)
        cursor = database._exec_sql(sql, arguments)
        result = translator.entity._fetch_objects(cursor, translator.attr_offsets)
        if translator.attrname is not None:
            return imap(attrgetter(translator.attrname), result)
        return iter(result)
    def orderby(query, *args):
        if not args: raise TypeError('query.orderby() requires at least one argument')
        entity = query._translator.entity
        order = []
        for arg in args:
            if isinstance(arg, orm.Attribute): order.append((arg, True))
            elif isinstance(arg, orm.DescWrapper): order.append((arg.attr, False))
            else: raise TypeError('query.orderby() arguments must be attributes. Got: %r' % arg)
            attr = order[-1][0]
            if entity._adict_.get(attr.name) is not attr: raise TypeError(
                'Attribute %s does not belong to Entity %s' % (attr, entity.__name__))
        new_query = object.__new__(Query)
        new_query.__dict__.update(query.__dict__)
        new_query._order = tuple(order)
        return new_query
    def __getitem__(query, key):
        if isinstance(key, slice):
            step = key.step
            if step is not None and step <> 1: raise TypeError("Parameter 'step' of slice object is not allowed here")
            start = key.start
            if start is None: start = 0
            elif start < 0: raise TypeError("Parameter 'start' of slice object cannot be negative")
            stop = key.stop
            if stop is None:
                if start is None: return query
                elif not query._limit: raise TypeError("Parameter 'stop' of slice object should be specified")
                else: stop = query._limit[1]
        else:
            try: i = key.__index__()
            except AttributeError:
                try: i = key.__int__()
                except AttributeError:
                    raise TypeError('Incorrect argument type: %r' % key)
            start = i
            stop = i + 1
        if query._limit is not None:
            prev_start, prev_stop = query._limit
            start = prev_start + start
            stop = min(prev_stop, prev_start + stop)
        if start >= stop: start = stop = 0
        new_query = object.__new__(Query)
        new_query.__dict__.update(query.__dict__)
        new_query._limit = start, stop
        return new_query
    def limit(query, limit, offset=None):
        start = offset or 0
        stop = start + limit
        return query[start:stop]
    def fetch(query):
        return list(query)

primitive_types = set([ int, unicode ])
type_normalization_dict = { long : int, str : unicode, StrHtml : unicode, Html : unicode }

def get_normalized_type(value):
    if isinstance(value, orm.EntityMeta): return value
    value_type = type(value)
    if value_type is orm.EntityIter: return value.entity
    return normalize_type(value_type)

def normalize_type(t):
    if t is NoneType: return t
    t = type_normalization_dict.get(t, t)
    if t not in primitive_types and not isinstance(t, orm.EntityMeta): raise TypeError, t
    return t

def are_comparable_types(op, type1, type2):
    # op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
    #         | 'in' | 'not' 'in' | 'is' | 'is' 'not'
    if op in ('is', 'is not'): return type1 is not NoneType and type2 is NoneType
    if op in ('<', '<=', '>', '>='): return type1 is type2 and type1 in primitive_types
    if op in ('==', '<>', '!='):
        if type1 is NoneType and type2 is NoneType: return False
        if type1 is NoneType or type2 is NoneType: return True
        elif type1 in primitive_types: return type1 is type2
        elif isinstance(type1, orm.EntityMeta):
            if not isinstance(type2, orm.EntityMeta): return False
            return type1._root_ is type2._root_
        else: return False
    else: assert False

def sqland(items):
    if len(items) == 1: return items[0]
    return [ AND ] + items

def sqlor(items):
    if len(items) == 1: return items[0]
    return [ OR ] + items

def join_tables(conditions, alias1, alias2, columns1, columns2):
    assert len(columns1) == len(columns2)
    conditions.extend([ EQ, [ COLUMN, alias1, c1 ], [ COLUMN, alias2, c2 ] ]
                     for c1, c2 in izip(columns1, columns2))

class ASTTranslator(object):
    def __init__(translator, tree):
        translator.tree = tree
        translator.pre_methods = {}
        translator.post_methods = {}
    def dispatch(translator, node):
        cls = node.__class__

        try: pre_method = translator.pre_methods[cls]
        except KeyError:
            pre_method = getattr(translator, 'pre' + cls.__name__, None)
            translator.pre_methods[cls] = pre_method
        if pre_method is not None:
            # print 'PRE', node.__class__.__name__, '+'
            stop = pre_method(node)
        else:            
            # print 'PRE', node.__class__.__name__, '-'
            stop = translator.default_pre(node)

        if stop: return
            
        for child in node.getChildNodes():
            translator.dispatch(child)

        try: post_method = translator.post_methods[cls]
        except KeyError:
            post_method = getattr(translator, 'post' + cls.__name__, None)
            translator.post_methods[cls] = post_method
        if post_method is not None:
            # print 'POST', node.__class__.__name__, '+'
            post_method(node)
        else:            
            # print 'POST', node.__class__.__name__, '-'
            translator.default_post(node)
    def default_pre(translator, node):
        pass
    def default_post(translator, node):
        pass

class SQLTranslator(ASTTranslator):
    def __init__(translator, tree, vartypes, functions, outer_iterables={}):
        assert isinstance(tree, ast.GenExprInner)
        ASTTranslator.__init__(translator, tree)
        translator.diagram = None
        translator.vartypes = vartypes
        translator.functions = functions
        translator.outer_iterables = outer_iterables
        translator.iterables = iterables = {}
        translator.aliases = aliases = {}
        translator.extractors = {}
        translator.distinct = False
        translator.from_ = [ FROM ]
        conditions = translator.conditions = []
        translator.inside_expr = False
        translator.alias_counters = {}
        for i, qual in enumerate(tree.quals):
            assign = qual.assign
            if not isinstance(assign, ast.AssName): raise TypeError
            if assign.flags != 'OP_ASSIGN': raise TypeError

            name = assign.name
            if name in iterables: raise TranslationError('Duplicate name: %s' % name)
            if name.startswith('__'): raise TranslationError('Illegal name: %s' % name)
            assert name not in aliases

            node = qual.iter
            attr_names = []
            while isinstance(node, ast.Getattr):
                attr_names.append(node.attrname)
                node = node.expr
            if not isinstance(node, ast.Name): raise TypeError

            if not attr_names:
                if i > 0: translator.distinct = True
                iter_name = node.name
                entity = vartypes[iter_name] # can raise KeyError
                if not isinstance(entity, orm.EntityMeta): raise NotImplementedError

                if translator.diagram is None: translator.diagram = entity._diagram_
                elif translator.diagram is not entity._diagram_: raise TranslationError(
                    'All entities in a query must belong to the same diagram')
            else:
                if len(attr_names) > 1: raise NotImplementedError
                attr_name = attr_names[0]
                parent_entity = iterables.get(node.name)
                if parent_entity is None: raise TranslationError("Name %r must be defined in query")
                attr = parent_entity._adict_.get(attr_name)
                if attr is None: raise AttributeError, attr_name
                if not attr.is_collection: raise TypeError
                if not isinstance(attr, orm.Set): raise NotImplementedError
                entity = attr.py_type
                if not isinstance(entity, orm.EntityMeta): raise NotImplementedError
                reverse = attr.reverse
                if not reverse.is_collection:
                    join_tables(conditions, node.name, name, parent_entity._pk_columns_, reverse.columns)
                else:
                    if not isinstance(reverse, orm.Set): raise NotImplementedError
                    translator.distinct = True
                    m2m_table = attr.table
                    m2m_alias = '%s--%s' % (node.name, name)
                    aliases[m2m_alias] = m2m_alias
                    translator.from_.append([ m2m_alias, TABLE, m2m_table ])
                    join_tables(conditions, node.name, m2m_alias, parent_entity._pk_columns_, reverse.columns)
                    join_tables(conditions, m2m_alias, name, attr.columns, entity._pk_columns_)
            iterables[name] = entity
            aliases[name] = name
            translator.from_.append([ name, TABLE, entity._table_ ])
            for if_ in qual.ifs:
                assert isinstance(if_, ast.GenExprIf)
                translator.dispatch(if_)
                translator.conditions.append(if_.monad.getsql())
        translator.inside_expr = True
        translator.dispatch(tree.expr)
        monad = tree.expr.monad
        translator.attrname = None
        if isinstance(monad, (StringAttrMonad, NumericAttrMonad)):
            translator.attrname = monad.attr.name
            monad = monad.parent
        if not isinstance(monad, (ObjectIterMonad, ObjectAttrMonad)):
            raise TranslationError, monad
        alias = monad.alias
        entity = translator.entity = monad.type
        if isinstance(monad, ObjectIterMonad):
            if alias != translator.tree.quals[-1].assign.name:
                translator.distinct = True
        elif isinstance(monad, ObjectAttrMonad):
            translator.distinct = True
            assert alias in aliases
        else: assert False
        short_alias = translator.alias = aliases[alias]
        translator.select, translator.attr_offsets = entity._construct_select_clause_(short_alias, translator.distinct)
        translator.sql_ast = [ SELECT, translator.select, translator.from_ ]
        if translator.conditions: translator.sql_ast.append([ WHERE, sqland(translator.conditions) ])
    def preGenExpr(translator, node):
        inner_tree = node.code
        outer_iterables = {}
        outer_iterables.update(translator.outer_iterables)
        outer_iterables.update(translator.iterables)
        subtranslator = SQLTranslator(inner_tree, translator.vartypes, translator.functions, outer_iterables)
        node.monad = QuerySetMonad(translator, subtranslator)
        return True
    def postGenExprIf(translator, node):
        monad = node.test.monad
        if monad.type is not bool: monad = monad.nonzero()
        node.monad = monad
    def postCompare(translator, node):
        expr1 = node.expr
        ops = node.ops
        if len(ops) > 1: raise NotImplementedError
        op, expr2 = ops[0]
        # op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
        #         | 'in' | 'not in' | 'is' | 'is not'
        if op.endswith('in'):
            node.monad = expr2.monad.contains(expr1.monad, op == 'not in')
        else:
            node.monad = expr1.monad.cmp(op, expr2.monad)
    def postConst(translator, node):
        value = node.value
        if type(value) is not tuple:
            node.monad = ConstMonad(translator, value)
        else:
            node.monad = ListMonad(translator, [ ConstMonad(translator, item) for item in value ])
    def postList(translator, node):
        node.monad = ListMonad(translator, [ item.monad for item in node.nodes ])
    def postTuple(translator, node):
        node.monad = ListMonad(translator, [ item.monad for item in node.nodes ])
    def postName(translator, node):
        name = node.name
        entity = translator.iterables.get(name)
        if entity is None:
            entity = translator.outer_iterables.get(name)
        if entity is not None:
            node.monad = ObjectIterMonad(translator, name, entity)
        else:
            try: value_type = translator.vartypes[name]
            except KeyError:
                func = translator.functions.get(name)
                if func is None: raise NameError(name)
                func_monad_class = special_functions[func]
                node.monad = func_monad_class(translator)
            else:
                if value_type is NoneType: node.monad = NoneMonad(translator)
                else: node.monad = ParamMonad(translator, value_type, name)
    def postAdd(translator, node):
        node.monad = node.left.monad + node.right.monad
    def postSub(translator, node):
        node.monad = node.left.monad - node.right.monad
    def postMul(translator, node):
        node.monad = node.left.monad * node.right.monad
    def postDiv(translator, node):
        node.monad = node.left.monad / node.right.monad
    def postPower(translator, node):
        node.monad = node.left.monad ** node.right.monad
    def postUnarySub(translator, node):
        node.monad = -node.expr.monad
    def postGetattr(translator, node):
        node.monad = node.expr.monad.getattr(node.attrname)
    def postAnd(translator, node):
        node.monad = AndMonad([ subnode.monad for subnode in node.nodes ])
    def postOr(translator, node):
        node.monad = OrMonad([ subnode.monad for subnode in node.nodes ])
    def postNot(translator, node):
        node.monad = node.expr.monad.negate()
    def preCallFunc(translator, node):
        if node.star_args is not None: raise NotImplementedError
        if node.dstar_args is not None: raise NotImplementedError
        if len(node.args) > 1: return False
        arg = node.args[0]
        if not isinstance(arg, ast.GenExpr): return False
        translator.dispatch(node.node)
        func_monad = node.node.monad
        translator.dispatch(arg)
        query_set_monad = arg.monad
        node.monad = func_monad(query_set_monad)
        return True
    def postCallFunc(translator, node):
        args = []
        keyargs = {}
        for arg in node.args:
            if isinstance(arg, ast.Keyword):
                keyargs[arg.name] = arg.expr.monad
            else: args.append(arg.monad)
        func_monad = node.node.monad
        node.monad = func_monad(*args, **keyargs)
    def postSubscript(translator, node):
        assert node.flags == 'OP_APPLY'
        assert isinstance(node.subs, list) and len(node.subs) == 1
        expr_monad = node.expr.monad
        index_monad = node.subs[0].monad
        node.monad = expr_monad[index_monad]
    def postSlice(translator, node):
        assert node.flags == 'OP_APPLY'
        expr_monad = node.expr.monad
        upper = node.upper
        if upper is not None: upper = upper.monad
        lower = node.lower
        if lower is not None: lower = lower.monad
        node.monad = expr_monad[lower:upper]
    def get_short_alias(translator, alias, entity_name):
        if alias and len(alias) <= MAX_ALIAS_LENGTH: return alias
        name = entity_name[:MAX_ALIAS_LENGTH-3].lower()
        i = translator.alias_counters.setdefault(name, 0) + 1
        short_alias = '%s-%d' % (name, i)
        translator.alias_counters[name] = i
        return short_alias

class Monad(object):
    def __init__(monad, translator, type):
        monad.translator = translator
        monad.type = type
    def cmp(monad, op, monad2):
        return CmpMonad(op, monad, monad2)
    def contains(monad, item, not_in=False): raise TypeError
    def nonzero(monad): raise TypeError
    def negate(monad):
        return NotMonad(monad)

    def getattr(monad, attrname): raise TypeError
    def __call__(monad, *args, **keyargs): raise TypeError
    def len(monad): raise TypeError
    def sum(monad): raise TypeError
    def min(monad): raise TypeError
    def max(monad): raise TypeError
    def __getitem__(monad, key): raise TypeError

    def __add__(monad, monad2): raise TypeError
    def __sub__(monad, monad2): raise TypeError
    def __mul__(monad, monad2): raise TypeError
    def __div__(monad, monad2): raise TypeError
    def __pow__(monad, monad2): raise TypeError

    def __neg__(monad): raise TypeError
    def abs(monad): raise TypeError

class ListMonad(Monad):
    def __init__(monad, translator, items):
        Monad.__init__(monad, translator, list)
        monad.items = items
    def contains(monad, x, not_in=False):
        for item in monad.items:
            if not are_comparable_types('==', x.type, item.type): raise TypeError
        left_sql = x.getsql()
        if len(left_sql) == 1:
            if not_in: sql = [ NOT_IN, left_sql[0], [ item.getsql()[0] for item in monad.items ] ]
            else: sql = [ IN, left_sql[0], [ item.getsql()[0] for item in monad.items ] ]
        elif not_in:
            sql = sqland([ sqlor([ [ NE, a, b ]  for a, b in zip(left_sql, item.getsql()) ]) for item in monad.items ])
        else:
            sql = sqlor([ sqland([ [ EQ, a, b ]  for a, b in zip(left_sql, item.getsql()) ]) for item in monad.items ])
        return BoolExprMonad(monad.translator, sql)

def make_numeric_binop(sqlop):
    def numeric_binop(monad, monad2):
        if not isinstance(monad2, NumericMixin): raise TypeError
        left_sql = monad.getsql()
        right_sql = monad2.getsql()
        assert len(left_sql) == len(right_sql) == 1
        return NumericExprMonad(monad.translator, [ sqlop, left_sql[0], right_sql[0] ])
    numeric_binop.__name__ = sqlop
    return numeric_binop

class NumericMixin(object):
    __add__ = make_numeric_binop(ADD)
    __sub__ = make_numeric_binop(SUB)
    __mul__ = make_numeric_binop(MUL)
    __div__ = make_numeric_binop(DIV)
    __pow__ = make_numeric_binop(POW)
    def __neg__(monad):
        sql = monad.getsql()[0]
        return NumericExprMonad(monad.translator, [ NEG, sql ])
    def abs(monad):
        sql = monad.getsql()[0]
        return NumericExprMonad(monad.translator, [ ABS, sql ])

def make_string_binop(sqlop):
    def string_binop(monad, monad2):
        if not isinstance(monad2, StringMixin): raise TypeError
        left_sql = monad.getsql()
        right_sql = monad2.getsql()
        assert len(left_sql) == len(right_sql) == 1
        return StringExprMonad(monad.translator, [ sqlop, left_sql[0], right_sql[0] ])
    string_binop.__name__ = sqlop
    return string_binop

class StringMixin(object):
    def getattr(monad, attrname):
        return StringMethodMonad(monad.translator, monad, attrname)
    __add__ = make_string_binop(CONCAT)
    def __getitem__(monad, index):
        if isinstance(index, slice):
            if index.step is not None: raise TypeError("Slice 'step' attribute is not supported")
            start, stop = index.start, index.stop
            if start is None and stop is None: return monad
            if isinstance(monad, StringConstMonad) \
               and (start is None or isinstance(start, NumericConstMonad)) \
               and (stop is None or isinstance(stop, NumericConstMonad)):
                if start is not None: start = start.value
                if stop is not None: stop = stop.value
                return StringConstMonad(monad.translator, monad.value[start:stop])

            if start is not None and start.type is not int: raise TypeError('string indices must be integers')
            if stop is not None and stop.type is not int: raise TypeError('string indices must be integers')
            
            expr_sql = monad.getsql()[0]

            if start is None:
                start_sql = [ VALUE, 1 ]
            elif isinstance(start, NumericConstMonad):
                if start.value < 0: raise NotImplementedError('Negative slice indices not supported')
                start_sql = [ VALUE, start.value + 1 ]
            else:
                start_sql = start.getsql()[0]
                start_sql = [ ADD, start_sql, [ VALUE, 1 ] ]

            if stop is None:
                len_sql = None
            elif isinstance(stop, NumericConstMonad):
                if stop.value < 0: raise NotImplementedError('Negative slice indices not supported')
                if start is None:
                    len_sql = [ VALUE, stop.value ]
                elif isinstance(start, NumericConstMonad):
                    len_sql = [ VALUE, stop.value - start.value ]
                else:
                    len_sql = [ SUB, [ VALUE, stop.value ], start_sql ]
            else:
                stop_sql = stop.getsql()[0]
                len_sql = [ SUB, stop_sql, start_sql ]

            sql = [ SUBSTR, expr_sql, start_sql, len_sql ]
            return StringExprMonad(monad.translator, sql)
        
        if isinstance(monad, StringConstMonad) and isinstance(index, NumericConstMonad):
            return StringConstMonad(monad.translator, monad.value[index.value])
        if index.type is not int: raise TypeError('string indices must be integers')
        expr_sql = monad.getsql()[0]
        if isinstance(index, NumericConstMonad):
            value = index.value
            if value >= 0: value += 1
            index_sql = [ VALUE, value ]
        else:
            inner_sql = index.getsql()[0]
            index_sql = [ ADD, inner_sql, [ CASE, None, [ ([GE, inner_sql, [ VALUE, 0 ]], [ VALUE, 1 ]) ], [ VALUE, 0 ] ] ]
        sql = [ SUBSTR, expr_sql, index_sql, [ VALUE, 1 ] ]
        return StringExprMonad(monad.translator, sql)
    def len(monad):
        sql = monad.getsql()[0]
        return NumericExprMonad(monad.translator, [ LENGTH, sql ])
    def contains(monad, item, not_in=False):
        if item.type is not unicode: raise TypeError
        if isinstance(item, StringConstMonad):
            item_sql = [ VALUE, '%%%s%%' % item.value ]
        else:
            item_sql = [ CONCAT, [ VALUE, '%' ], item.getsql()[0], [ VALUE, '%' ] ]
        sql = [ LIKE, monad.getsql()[0], item_sql ]
        return BoolExprMonad(monad.translator, sql)
        
class MethodMonad(Monad):
    def __init__(monad, translator, parent, attrname):
        Monad.__init__(monad, translator, 'METHOD')
        monad.parent = parent
        monad.attrname = attrname
        try: method = getattr(monad, 'call_' + monad.attrname)
        except AttributeError:
            raise AttributeError('%r object has no attribute %r' % (parent.type.__name__, attrname))
    def __call__(monad, *args, **keyargs):
        method = getattr(monad, 'call_' + monad.attrname)
        return method(*args, **keyargs)

def make_string_func(sqlop):
    def func(monad):
        sql = monad.parent.getsql()
        assert len(sql) == 1
        return StringExprMonad(monad.translator, [ sqlop, sql[0] ])
    func.__name__ = sqlop
    return func

class StringMethodMonad(MethodMonad):
    call_upper = make_string_func(UPPER)
    call_lower = make_string_func(LOWER)
    def call_startswith(monad, arg):
        parent_sql = monad.parent.getsql()[0]
        if arg.type is not unicode:
            raise TypeError("Argument of 'startswith' method must be a string")
        if isinstance(arg, StringConstMonad):
            assert isinstance(arg.value, basestring)
            arg_sql = [ VALUE, arg.value + '%' ]
        else:
            arg_sql = arg.getsql()[0]
            arg_sql = [ CONCAT, arg_sql, [ VALUE, '%' ] ]
        sql = [ LIKE, parent_sql, arg_sql ]
        return BoolExprMonad(monad.translator, sql)
    def call_endswith(monad, arg):
        parent_sql = monad.parent.getsql()[0]
        if arg.type is not unicode:
            raise TypeError("Argument of 'endswith' method must be a string")
        if isinstance(arg, StringConstMonad):
            assert isinstance(arg.value, basestring)
            arg_sql = [ VALUE, '%' + arg.value ]
        else:
            arg_sql = arg.getsql()[0]
            arg_sql = [ CONCAT, [ VALUE, '%' ], arg_sql ]
        sql = [ LIKE, parent_sql, arg_sql ]
        return BoolExprMonad(monad.translator, sql)
    def strip(monad, chars, strip_type):
        parent_sql = monad.parent.getsql()[0]
        if chars is not None and chars.type is not unicode:
            raise TypeError("'chars' argument must be a string")
        if chars is None:
            return StringExprMonad(monad.translator, [ strip_type, parent_sql ])
        else:
            chars_sql = chars.getsql()[0]
            return StringExprMonad(monad.translator, [ strip_type, parent_sql, chars_sql ])
    def call_strip(monad, chars=None):
        return monad.strip(chars, TRIM)
    def call_lstrip(monad, chars=None):
        return monad.strip(chars, LTRIM)
    def call_rstrip(monad, chars=None):
        return monad.strip(chars, RTRIM)
    
class ObjectMixin(object):
    def getattr(monad, name):
        translator = monad.translator
        entity = monad.type
        attr = getattr(entity, name) # can raise AttributeError
        if attr.is_collection:
            return AttrSetMonad(monad, [ attr ])
        else:
            return AttrMonad.new(monad, attr)

class ObjectIterMonad(ObjectMixin, Monad):
    def __init__(monad, translator, alias, entity):
        Monad.__init__(monad, translator, entity)
        monad.alias = alias
    def getsql(monad):
        entity = monad.type
        return [ [ COLUMN, monad.alias, column ] for attr in entity._pk_attrs_ if not attr.is_collection
                                                 for column in attr.columns ]

class AttrMonad(Monad):
    @staticmethod
    def new(parent, attr, *args, **keyargs):
        type = normalize_type(attr.py_type)
        if type is int: cls = NumericAttrMonad
        elif type is unicode: cls = StringAttrMonad
        elif isinstance(type, orm.EntityMeta): cls = ObjectAttrMonad
        else: raise NotImplementedError
        return cls(parent, attr, *args, **keyargs)
    def getsql(monad):
        return [ [ COLUMN, monad.parent.alias, column ] for column in monad.attr.columns ]
    def __init__(monad, parent, attr):
        assert monad.__class__ is not AttrMonad
        attr_type = normalize_type(attr.py_type)
        Monad.__init__(monad, parent.translator, attr_type)
        monad.parent = parent
        monad.attr = attr
        monad.alias = None
        
class ObjectAttrMonad(ObjectMixin, AttrMonad):
    def __init__(monad, parent, attr):
        AttrMonad.__init__(monad, parent, attr)
        monad.alias = '-'.join((parent.alias, attr.name))
        monad._make_join()
    def _make_join(monad):
        translator = monad.translator
        parent = monad.parent
        attr = monad.attr
        alias = monad.alias
        entity = monad.type

        short_alias = translator.aliases.get(alias)
        if short_alias is not None: return
        short_alias = translator.get_short_alias(alias, entity.__name__)
        translator.aliases[alias] = short_alias
        translator.from_.append([ short_alias, TABLE, entity._table_ ])
        join_tables(translator.conditions, parent.alias, short_alias, attr.columns, entity._pk_columns_)
        
class NumericAttrMonad(NumericMixin, AttrMonad): pass
class StringAttrMonad(StringMixin, AttrMonad): pass

class ParamMonad(Monad):
    def __new__(cls, translator, type, name, parent=None):
        assert cls is ParamMonad
        type = normalize_type(type)
        if type is int: cls = NumericParamMonad
        elif type is unicode: cls = StringParamMonad
        elif isinstance(type, orm.EntityMeta): cls = ObjectParamMonad
        else: assert False
        return object.__new__(cls)
    def __init__(monad, translator, type, name, parent=None):
        type = normalize_type(type)
        Monad.__init__(monad, translator, type)
        monad.name = name
        monad.parent = parent
        if parent is None: monad.extractor = lambda variables : variables[name]
        else: monad.extractor = lambda variables : getattr(parent.extractor(variables), name)
    def getsql(monad):
        monad.add_extractors()
        return [ [ PARAM, monad.name ] ]
    def add_extractors(monad):
        name = monad.name
        extractors = monad.translator.extractors
        extractors[name] = monad.extractor

class ObjectParamMonad(ObjectMixin, ParamMonad):
    def __init__(monad, translator, entity, name, parent=None):
        if translator.diagram is not entity._diagram_: raise TranslationError(
            'All entities in a query must belong to the same diagram')
        monad.params = [ '-'.join((name, path)) for path in entity._pk_paths_ ]
        ParamMonad.__init__(monad, translator, entity, name, parent)
    def getattr(monad, name):
        entity = monad.type
        attr = entity._adict_[name]
        return ParamMonad(monad.translator, attr.py_type, name, monad)
    def getsql(monad):
        monad.add_extractors()
        return [ [ PARAM, param ] for param in monad.params ]
    def add_extractors(monad):
        entity = monad.type
        extractors = monad.translator.extractors
        if not entity._raw_pk_is_composite_:
            extractors[monad.params[0]] = lambda variables, extractor=monad.extractor : extractor(variables)._raw_pkval_
        else:
            for i, param in enumerate(monad.params):
                extractors[param] = lambda variables, i=i, extractor=monad.extractor : extractor(variables)._raw_pkval_[i]

class StringParamMonad(StringMixin, ParamMonad): pass
class NumericParamMonad(NumericMixin, ParamMonad): pass

class ExprMonad(Monad):
    @staticmethod
    def new(translator, sql, type):
        if type is int: cls = NumericExprMonad
        elif type is unicode: cls = StringExprMonad
        else: raise NotImplementedError
        return cls(translator, sql)
    def __init__(monad, translator, sql, type):
        Monad.__init__(monad, translator, type)
        monad.sql = sql
    def getsql(monad):
        return [ monad.sql ]

class StringExprMonad(StringMixin, ExprMonad):
    def __init__(monad, translator, sql):
        ExprMonad.__init__(monad, translator, sql, unicode)
        
class NumericExprMonad(NumericMixin, ExprMonad):
    def __init__(monad, translator, sql):
        ExprMonad.__init__(monad, translator, sql, int)

class ConstMonad(Monad):
    def __new__(cls, translator, value):
        assert cls is ConstMonad
        value_type = normalize_type(type(value))
        if value_type is int: cls = NumericConstMonad
        elif value_type is unicode: cls = StringConstMonad
        elif value_type is NoneType: cls = NoneMonad
        else: raise TypeError
        return object.__new__(cls)
    def __init__(monad, translator, value):
        value_type = normalize_type(type(value))
        Monad.__init__(monad, translator, value_type)
        monad.value = value
    def getsql(monad):
        return [ [ VALUE, monad.value ] ]

class NoneMonad(Monad):
    type = NoneType
    def __init__(monad, translator, value=None):
        assert value is None
        ConstMonad.__init__(monad, translator, value)

class StringConstMonad(StringMixin, ConstMonad):
    def len(monad):
        return NumericExprMonad(monad.translator, [ VALUE, len(monad.value) ])
    
class NumericConstMonad(NumericMixin, ConstMonad): pass

class BoolMonad(Monad):
    def __init__(monad, translator):
        monad.translator = translator
        monad.type = bool

sql_negation = { IN : NOT_IN, EXISTS : NOT_EXISTS, LIKE : NOT_LIKE, BETWEEN : NOT_BETWEEN, IS_NULL : IS_NOT_NULL }
sql_negation.update((value, key) for key, value in sql_negation.items())

class BoolExprMonad(BoolMonad):
    def __init__(monad, translator, sql):
        monad.translator = translator
        monad.type = bool
        monad.sql = sql
    def getsql(monad):
        return monad.sql
    def negate(monad):
        sql = monad.sql
        sqlop = sql[0]
        negated_op = sql_negation.get(sqlop)
        if negated_op is not None:
            negated_sql = [ negated_op ] + sql[1:]
        elif negated_op == NOT:
            assert len(sql) == 2
            negated_sql = sql[1]
        else:
            return NotMonad(monad.translator, sql)
        return BoolExprMonad(monad.translator, negated_sql)

cmpops = { '>=' : GE, '>' : GT, '<=' : LE, '<' : LT }        

class CmpMonad(BoolMonad):
    def __init__(monad, op, left, right):
        if not are_comparable_types(op, left.type, right.type): raise TypeError, [left.type, right.type]
        if op == '<>': op = '!='
        if left.type is NoneType:
            assert right.type is not NoneType
            left, right = right, left
        if right.type is NoneType:
            if op == '==': op = 'is'
            elif op == '!=': op = 'is not'
        elif op == 'is': op = '=='
        elif op == 'is not': op = '!='
        BoolMonad.__init__(monad, left.translator)
        monad.op = op
        monad.left = left
        monad.right = right
    def getsql(monad):
        op = monad.op
        sql = []
        left_sql = monad.left.getsql()
        if op == 'is':
            return sqland([ [ IS_NULL, item ] for item in left_sql ])
        if op == 'is not':
            return sqland([ [ IS_NOT_NULL, item ] for item in left_sql ])
        right_sql = monad.right.getsql()
        assert len(left_sql) == len(right_sql)
        if op in ('<', '<=', '>', '>='):
            assert len(left_sql) == len(right_sql) == 1
            return [ cmpops[op], left_sql[0], right_sql[0] ]
        if op == '==':
            return sqland([ [ EQ, a, b ] for (a, b) in zip(left_sql, right_sql) ])
        if op == '!=':
            return sqlor([ [ NE, a, b ] for (a, b) in zip(left_sql, right_sql) ])
        assert False

class LogicalBinOpMonad(BoolMonad):
    def __init__(monad, operands):
        assert len(operands) >= 2
        for operand in operands:
            if operand.type is not bool: raise TypeError
        BoolMonad.__init__(monad, operands[0].translator)
        monad.operands = operands
    def getsql(monad):
        return [ monad.binop ] + [ operand.getsql() for operand in monad.operands ]

class AndMonad(LogicalBinOpMonad):
    binop = AND

class OrMonad(LogicalBinOpMonad):
    binop = OR

class NotMonad(BoolMonad):
    def __init__(monad, operand):
        if operand.type is not bool: operand = operand.nonzero()
        BoolMonad.__init__(monad, operand.translator)
        monad.operand = operand
    def negate(monad):
        return monad.operand
    def getsql(monad):
        return [ NOT, monad.operand.getsql() ]

class FuncMonad(Monad):
    type = None
    def __init__(monad, translator):
        monad.translator = translator

def func_monad(type):
    def decorator(monad_func):
        class SpecificFuncMonad(FuncMonad):
            def __call__(monad, *args, **keyargs):
                for arg in args:
                    assert isinstance(arg, Monad)
                for value in keyargs.values():
                    assert isinstance(value, Monad)
                return monad_func(monad, *args, **keyargs)
        SpecificFuncMonad.type = type
        SpecificFuncMonad.__name__ = monad_func.__name__
        return SpecificFuncMonad
    return decorator

@func_monad(type=int)
def FuncLenMonad(monad, x):
    return x.len()

@func_monad(type=int)
def FuncAbsMonad(monad, x):
    return x.abs()

@func_monad(type=int)
def FuncSumMonad(monad, x):
    return x.sum()

@func_monad(type=None)
def FuncMinMonad(monad, *args):
    if not args: raise TypeError
    if len(args) == 1: return args[0].min()
    return minmax(monad, MIN, *args)

@func_monad(type=None)
def FuncMaxMonad(monad, *args):
    if not args: raise TypeError
    if len(args) == 1: return args[0].max()
    return minmax(monad, MAX, *args)

def minmax(monad, sqlop, *args):
    assert len(args) > 1
    sql = [ sqlop ] + [ arg.getsql()[0] for arg in args ]
    arg_types = set(arg.type for arg in args)
    if len(arg_types) > 1: raise TypeError
    result_type = arg_types.pop()
    if result_type is int:
        return NumericExprMonad(monad.translator, sql)
    elif result_type is unicode:
        return StringExprMonad(monad.translator, sql)
    else: raise TypeError

class SetMixin(object):
    pass

class AttrSetMonad(SetMixin, Monad):
    def __init__(monad, root, path):
        if root.translator.inside_expr: raise NotImplementedError
        item_type = normalize_type(path[-1].py_type)
        Monad.__init__(monad, root.translator, (item_type,))
        monad.root = root
        monad.path = path
    def cmp(monad, op, monad2):
        raise NotImplementedError
    def contains(monad, item, not_in=False):
        item_type = monad.type[0]
        if not are_comparable_types('==', item_type, item.type): raise TypeError
        if isinstance(item_type, orm.EntityMeta) and len(item_type._pk_columns_) > 1:
            raise NotImplementedError

        expr, from_ast, conditions = monad._subselect()
        if expr is None:
            assert isinstance(item_type, orm.EntityMeta)
            expr = [ COLUMN, alias, item_type._pk_columns_[0] ]
        subquery_ast = [ SELECT, [ ALL, expr ], from_ast, [ WHERE, sqland(conditions) ] ]
        sqlop = not_in and NOT_IN or IN
        return BoolExprMonad(monad.translator, [ sqlop, item.getsql()[0], subquery_ast ])
    def getattr(monad, name):
        item_type = monad.type[0]
        if not isinstance(item_type, orm.EntityMeta):
            raise AttributeError, name
        entity = item_type
        attr = entity._adict_.get(name)
        if attr is None: raise AttributeError, name
        return AttrSetMonad(monad.root, monad.path + [ attr ])
    def len(monad):
        if not monad.path[-1].reverse: kind = DISTINCT
        else: kind = ALL
        expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [ COUNT, kind, expr ] ], from_ast, [ WHERE, sqland(conditions) ] ]
        return NumericExprMonad(monad.translator, sql_ast)
    def sum(monad):
        if monad.type[0] is not int: raise TypeError
        expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [COALESCE, [ SUM, expr ], [ VALUE, 0 ]]], from_ast, [ WHERE, sqland(conditions) ] ]
        return NumericExprMonad(monad.translator, sql_ast)
    def min(monad):
        item_type = monad.type[0]
        if item_type not in (int, unicode): raise TypeError
        expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [ MIN, expr ] ], from_ast, [ WHERE, sqland(conditions) ] ]
        return ExprMonad.new(monad.translator, sql_ast, item_type)
    def max(monad):
        item_type = monad.type[0]
        if item_type not in (int, unicode): raise TypeError
        expr, from_ast, conditions = monad._subselect()
        sql_ast = [ SELECT, [ AGGREGATES, [ MAX, expr ] ], from_ast, [ WHERE, sqland(conditions) ] ]
        return ExprMonad.new(monad.translator, sql_ast, item_type)
    def nonzero(monad):
        expr, from_ast, conditions = monad._subselect()
        sql_ast = [ EXISTS, from_ast, [ WHERE, sqland(conditions) ] ]
        return BoolExprMonad(monad.translator, sql_ast)
    def negate(monad):
        expr, from_ast, conditions = monad._subselect()
        sql_ast = [ NOT_EXISTS, from_ast, [ WHERE, sqland(conditions) ] ]
        return BoolExprMonad(monad.translator, sql_ast)
    def _subselect(monad):
        from_ast = [ FROM ]
        conditions = []
        alias = None
        prev_alias = monad.root.alias
        expr = None 
        for attr in monad.path:
            prev_entity = attr.entity
            reverse = attr.reverse
            if not reverse:
                assert len(attr.columns) == 1
                expr = [ COLUMN, alias, attr.column ]
                assert attr is monad.path[-1]
                break
            
            next_entity = attr.py_type
            assert isinstance(next_entity, orm.EntityMeta)
            alias = '-'.join((prev_alias, attr.name))
            alias = monad.translator.get_short_alias(alias, next_entity.__name__)
            if not attr.is_collection:
                from_ast.append([ alias, TABLE, next_entity._table_ ])
                if attr.columns:                    
                    join_tables(conditions, prev_alias, alias, attr.columns, next_entity._pk_columns_)
                else:
                    assert not reverse.is_collection and reverse.columns
                    join_tables(conditions, prev_alias, alias, prev_entity._pk_columns_, reverse.columns)
            elif reverse.is_collection:
                m2m_table = attr.table
                m2m_alias = monad.translator.get_short_alias(None, 'm2m-')
                from_ast.append([ m2m_alias, TABLE, m2m_table ])
                join_tables(conditions, prev_alias, m2m_alias, prev_entity._pk_columns_, reverse.columns)
                from_ast.append([ alias, TABLE, next_entity._table_ ])
                join_tables(conditions, m2m_alias, alias, attr.columns, next_entity._pk_columns_)
            else:
                from_ast.append([ alias, TABLE, next_entity._table_ ])
                join_tables(conditions, prev_alias, alias, prev_entity._pk_columns_, reverse.columns)
            prev_alias = alias
        assert alias is not None
        return expr, from_ast, conditions
    def getsql(monad):
        raise TranslationError

@func_monad(type=None)
def FuncSelectMonad(monad, subquery):
    if not isinstance(subquery, QuerySetMonad): raise TypeError
    return subquery

class QuerySetMonad(SetMixin, Monad):
    def __init__(monad, translator, subtranslator):        
        monad.subtranslator = subtranslator
        attr, attr_type = monad._get_attr_info()
        item_type = attr_type or subtranslator.entity
        monad.item_type = item_type
        Monad.__init__(monad, translator, (item_type,))
    def _get_attr_info(monad):
        subtranslator = monad.subtranslator
        attrname = subtranslator.attrname
        if attrname is None: return None, None
        entity = subtranslator.entity
        attr = entity._adict_[attrname]
        attr_type = normalize_type(attr.py_type)
        return attr, attr_type
    def _subselect(monad, select_ast, item_type):
        from_ast = monad.subtranslator.from_
        where_ast = [ WHERE, sqland(monad.subtranslator.conditions) ]
        sql_ast = [ SELECT, select_ast, from_ast, where_ast ]
        return ExprMonad.new(monad.translator, sql_ast, item_type)
    def contains(monad, item, not_in=False):
        item_type = monad.type[0]
        if not are_comparable_types('==', item_type, item.type): raise TypeError, [ item_type, item.type ]
        attr, attr_type = monad._get_attr_info()
        sub = monad.subtranslator
        if attr is None: columns = item_type._pk_columns_
        else: columns = attr.columns
        if len(columns) > 1: raise NotImplementedError
        select_ast = [ ALL, [ COLUMN, sub.alias, columns[0] ] ]
        subquery_ast = [ SELECT, select_ast, sub.from_, [ WHERE, sqland(sub.conditions) ] ]
        sqlop = not_in and NOT_IN or IN
        return BoolExprMonad(monad.translator, [ sqlop, item.getsql()[0], subquery_ast ])
    def len(monad):
        attr, attr_type = monad._get_attr_info()
        if attr is None:
            select_ast = [ AGGREGATES, [ COUNT, ALL ] ]
        else:            
            if len(attr.columns) > 1: raise NotImplementedError
            select_ast = [ AGGREGATES, [ COUNT, DISTINCT, [ COLUMN, monad.subtranslator.alias, attr.column ] ] ]
        return monad._subselect(select_ast, int)
    def sum(monad):
        attr, attr_type = monad._get_attr_info()
        if attr_type is not int: raise TypeError
        select_ast = [ AGGREGATES, [ COALESCE, [ SUM, [ COLUMN, monad.subtranslator.alias, attr.column ] ], [ VALUE, 0 ] ] ]
        return monad._subselect(select_ast, int)
    def min(monad):
        attr, attr_type = monad._get_attr_info()
        if attr_type not in (int, unicode): raise TypeError
        select_ast = [ AGGREGATES, [ MIN, [ COLUMN, monad.subtranslator.alias, attr.column ] ] ]
        return monad._subselect(select_ast, attr_type)
    def max(monad):
        attr, attr_type = monad._get_attr_info()
        if attr_type not in (int, unicode): raise TypeError
        select_ast = [ AGGREGATES, [ MAX, [ COLUMN, monad.subtranslator.alias, attr.column ] ] ]
        return monad._subselect(select_ast, attr_type)
    def nonzero(monad):        
        from_ast = monad.subtranslator.from_
        where_ast = [ WHERE, sqland(monad.subtranslator.conditions) ]
        sql_ast = [ EXISTS, from_ast, where_ast ]
        return BoolExprMonad(monad.translator, sql_ast)
    def negate(monad):
        from_ast = monad.subtranslator.from_
        where_ast = [ WHERE, sqland(monad.subtranslator.conditions) ]
        sql_ast = [ NOT_EXISTS, from_ast, where_ast ]
        return BoolExprMonad(monad.translator, sql_ast)

@func_monad(type=None)
def FuncExistsMonad(monad, subquery):
    if not isinstance(subquery, SetMixin): raise TypeError
    return subquery.nonzero()

special_functions = {
    len : FuncLenMonad,
    abs : FuncAbsMonad,
    min : FuncMinMonad,
    max : FuncMaxMonad,
    sum : FuncSumMonad,
    select : FuncSelectMonad,
    exists : FuncExistsMonad,
}