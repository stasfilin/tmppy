#  Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import textwrap
import _py2tmp.ir as ir
import _py2tmp.types as types
from _py2tmp.utils import ast_to_string
import typed_ast.ast3 as ast
from typing import List, Tuple, Callable, Dict, Optional

class Symbol:
    def __init__(self, name: str, type: types.ExprType):
        self.type = type
        self.name = name

class SymbolTable:
    def __init__(self, parent=None):
        self.symbols_by_name = dict() # type: Dict[str, Tuple[Symbol, ast.AST]]
        self.parent = parent

    def get_symbol_definition(self, name: str) -> Tuple[Optional[Symbol], Optional[ast.AST]]:
        result = self.symbols_by_name.get(name)
        if result:
            return result
        if self.parent:
            return self.parent.get_symbol_definition(name)
        return None, None

    def add_symbol(self, name: str, type: types.ExprType, definition_ast_node: ast.AST, compilation_context, allow_redefinition_with_same_type):
        '''
        Adds a symbol to the symbol table.

        This throws an error (created by calling `create_already_defined_error(previous_type)`) if a symbol with the
        same name and different type was already defined in this scope.
        '''
        previous_symbol, previous_definition_ast_node = self.symbols_by_name.get(name, (None, None))
        if previous_symbol:
            assert isinstance(previous_definition_ast_node, ast.AST)
            if not allow_redefinition_with_same_type:
                raise CompilationError(compilation_context,
                                       definition_ast_node,
                                       '%s was already defined in this scope.' % definition_ast_node.name,
                                       notes=[(previous_definition_ast_node, 'The previous declaration was here.')])
            elif previous_symbol.type != type:
                raise CompilationError(compilation_context,
                                       definition_ast_node,
                                       '%s was already defined in this scope, with type %s; we can\'t re-define it here with type %s.' % (
                                           definition_ast_node.name, str(previous_symbol.type), str(type)),
                                       notes=[(previous_definition_ast_node, 'The previous declaration was here.')])
        self.symbols_by_name[name] = (Symbol(name, type), definition_ast_node)

class CompilationContext:
    def __init__(self, symbol_table: SymbolTable, filename: str, source_lines: List[str]):
        self.symbol_table = symbol_table
        self.filename = filename
        self.source_lines = source_lines

class CompilationError(Exception):
    def __init__(self, compilation_context: CompilationContext, ast_node: ast.AST, error_message: str, notes: List[Tuple[ast.AST, str]] = []):
        error_message = CompilationError._diagnostic_to_string(compilation_context=compilation_context,
                                                               ast_node=ast_node,
                                                               diagnostic_kind='error',
                                                               message=error_message)
        notes = [CompilationError._diagnostic_to_string(compilation_context=compilation_context,
                                                        ast_node=note_ast_node,
                                                        diagnostic_kind='note',
                                                        message=note_message)
                 for note_ast_node, note_message in notes]
        super().__init__(''.join([error_message] + notes))

    @staticmethod
    def _diagnostic_to_string(compilation_context: CompilationContext, ast_node: ast.AST, diagnostic_kind: str, message: str):
        first_line_number = ast_node.lineno
        first_column_number = ast_node.col_offset
        error_marker = ' ' * first_column_number + '^'
        return textwrap.dedent('''\
            {filename}:{first_line_number}:{first_column_number}: {diagnostic_kind}: {message}
            {line}
            {error_marker}
            ''').format(filename=compilation_context.filename,
                        first_line_number=first_line_number,
                        first_column_number=first_column_number,
                        diagnostic_kind=diagnostic_kind,
                        message=message,
                        line=compilation_context.source_lines[first_line_number - 1],
                        error_marker=error_marker)

def module_ast_to_ir(module_ast_node: ast.Module, compilation_context: CompilationContext):
    function_defns = []
    for ast_node in module_ast_node.body:
        if isinstance(ast_node, ast.FunctionDef):
            function_defn = function_def_ast_to_ir(ast_node, compilation_context)
            function_defns.append(function_defn)
            compilation_context.symbol_table.add_symbol(
                name=ast_node.name,
                type=function_defn.type,
                definition_ast_node=ast_node,
                compilation_context=compilation_context,
                allow_redefinition_with_same_type=False)
        elif isinstance(ast_node, ast.ImportFrom):
            if ast_node.module == 'tmppy':
                if (len(ast_node.names) != 1
                    or not isinstance(ast_node.names[0], ast.alias)
                    or ast_node.names[0].name != 'Type'
                    or ast_node.names[0].asname):
                    raise CompilationError(compilation_context, ast_node, 'The only supported import from mypl is Type.')
            elif ast_node.module == 'typing':
                if (len(ast_node.names) != 1
                    or not isinstance(ast_node.names[0], ast.alias)
                    or ast_node.names[0].name not in ('List', 'Callable')
                    or ast_node.names[0].asname):
                    raise CompilationError(compilation_context, ast_node, 'The only supported imports from typing are List and Callable.')
        else:
            raise CompilationError(compilation_context, ast_node, 'Unsupported AST node in MypyFile: ' + str(type(ast_node)))
    return ir.Module(function_defns=function_defns)

def function_def_ast_to_ir(ast_node: ast.FunctionDef, compilation_context: CompilationContext):
    function_body_compilation_context = CompilationContext(SymbolTable(parent=compilation_context.symbol_table),
                                                           compilation_context.filename,
                                                           compilation_context.source_lines)
    args = []
    for arg in ast_node.args.args:
        if arg.type_comment:
            raise CompilationError(compilation_context, arg, 'Type comments (as opposed to annotations) for function arguments are not supported.')
        if not arg.annotation:
            raise CompilationError(compilation_context, arg, 'All function arguments must have a type annotation.')
        arg_type = type_declaration_ast_to_ir_expression_type(arg.annotation, compilation_context)
        function_body_compilation_context.symbol_table.add_symbol(
            name=arg.arg,
            type=arg_type,
            definition_ast_node=arg,
            compilation_context=compilation_context,
            allow_redefinition_with_same_type=False)
        args.append(ir.FunctionArgDecl(type=arg_type, name=arg.arg))
    if not args:
        raise CompilationError(compilation_context, ast_node, 'Functions with no arguments are not supported.')

    if ast_node.args.vararg:
        raise CompilationError(compilation_context, ast_node, 'Function vararg arguments are not supported.')
    if ast_node.args.kwonlyargs:
        raise CompilationError(compilation_context, ast_node, 'Keyword-only function arguments are not supported.')
    if ast_node.args.kw_defaults or ast_node.args.defaults:
        raise CompilationError(compilation_context, ast_node, 'Default values for function arguments are not supported.')
    if ast_node.args.kwarg:
        raise CompilationError(compilation_context, ast_node, 'Keyword function arguments are not supported.')
    if ast_node.decorator_list:
        raise CompilationError(compilation_context, ast_node, 'Function decorators are not supported.')
    if ast_node.returns:
        raise CompilationError(compilation_context, ast_node, 'Return type annotations for functions are not supported.')

    asserts = []
    for statement_node in ast_node.body[:-1]:
        if not isinstance(statement_node, ast.Assert):
            raise CompilationError(compilation_context, statement_node, 'All statements in a function (except the last) must be assertions.')
        asserts.append(assert_ast_to_ir(statement_node, function_body_compilation_context))
    if not isinstance(ast_node.body[-1], ast.Return):
        raise CompilationError(compilation_context, ast_node.body[-1],
            'The last statement in a function must be a Return statement. Got: ' + str(type(ast_node.body[-1])))
    expression = ast_node.body[-1].value
    if not expression:
        raise CompilationError(compilation_context, ast_node.body[-1], 'Return statements with no returned expression are not supported.')
    expression = expression_ast_to_ir(expression, function_body_compilation_context)
    fun_type = types.FunctionType(argtypes=[arg.type for arg in args],
                                  returns=expression.type)

    return ir.FunctionDefn(
        asserts=asserts,
        expression=expression,
        name=ast_node.name,
        type=fun_type,
        args=args)

def assert_ast_to_ir(ast_node: ast.Assert, compilation_context: CompilationContext):
    expr = expression_ast_to_ir(ast_node.test, compilation_context)
    assert expr.type.kind == types.ExprKind.BOOL

    if ast_node.msg:
        assert isinstance(ast_node.msg, ast.Str)
        message = ast_node.msg.s
    else:
        message = 'Assertion error'

    return ir.StaticAssert(expr=expr, message=message)

def compare_ast_to_ir(ast_node: ast.Compare, compilation_context: CompilationContext):
    if len(ast_node.ops) == 1 and isinstance(ast_node.ops[0], ast.Eq):
        if len(ast_node.comparators) != 1:
            raise CompilationError(compilation_context, ast_node, 'Expected exactly 1 comparator in expression, but got %s' % len(ast_node.comparators))
        return eq_ast_to_ir(ast_node.left, ast_node.comparators[0], compilation_context)
    else:
        raise CompilationError(compilation_context, ast_node, 'Comparison not supported.')  # pragma: no cover

def expression_ast_to_ir(ast_node: ast.AST, compilation_context: CompilationContext):
    if isinstance(ast_node, ast.NameConstant):
        return name_constant_ast_to_ir(ast_node, compilation_context)
    elif isinstance(ast_node, ast.Call) and ast_node.func.id == 'Type':
        return type_literal_ast_to_ir(ast_node, compilation_context)
    elif isinstance(ast_node, ast.Call):
        return function_call_ast_to_ir(ast_node, compilation_context)
    elif isinstance(ast_node, ast.Compare):
        return compare_ast_to_ir(ast_node, compilation_context)
    elif isinstance(ast_node, ast.Name) and isinstance(ast_node.ctx, ast.Load):
        return var_reference_ast_to_ir(ast_node, compilation_context)
    elif isinstance(ast_node, ast.List) and isinstance(ast_node.ctx, ast.Load):
        return list_expression_ast_to_ir(ast_node, compilation_context)
    else:
        raise CompilationError(compilation_context, ast_node, 'Unsupported expression type.')  # pragma: no cover

def name_constant_ast_to_ir(ast_node: ast.NameConstant, compilation_context: CompilationContext):
    if ast_node.value in (True, False):
        type = types.BoolType()
    else:
        raise CompilationError(compilation_context, ast_node, 'NameConstant not supported: ' + str(ast_node.value))  # pragma: no cover

    return ir.Literal(
        value=ast_node.value,
        type=type)

def type_literal_ast_to_ir(ast_node: ast.Call, compilation_context: CompilationContext):
    if len(ast_node.args) != 1:
        raise CompilationError(compilation_context, ast_node, 'Type() takes exactly 1 argument. Got: %s' % len(ast_node.args))
    [arg] = ast_node.args
    if not isinstance(arg, ast.Str):
        raise CompilationError(compilation_context, arg, 'The first argument to Type should be a string constant, but was: ' + ast_to_string(arg))
    return ir.TypeLiteral(cpp_type=arg.s)

def eq_ast_to_ir(lhs_node: ast.AST, rhs_node: ast.AST, compilation_context: CompilationContext):
    lhs = expression_ast_to_ir(lhs_node, compilation_context)
    rhs = expression_ast_to_ir(rhs_node, compilation_context)
    if lhs.type != rhs.type:
        raise CompilationError(compilation_context, lhs_node, 'Type mismatch in ==: %s vs %s' % (
            str(lhs.type), str(rhs.type)))
    if lhs.type.kind not in (types.ExprKind.BOOL, types.ExprKind.TYPE):
        raise CompilationError(compilation_context, lhs_node, 'Type not supported in equality comparison: ' + str(lhs.type))
    return ir.EqualityComparison(lhs=lhs, rhs=rhs)

def function_call_ast_to_ir(ast_node: ast.Call, compilation_context: CompilationContext):
    fun_expr = expression_ast_to_ir(ast_node.func, compilation_context)
    if not isinstance(fun_expr.type, types.FunctionType):
        raise CompilationError(compilation_context, ast_node,
                               'Attempting to call an object that is not a function. It has type: %s' % str(fun_expr.type))

    args = [expression_ast_to_ir(arg_node, compilation_context) for arg_node in ast_node.args]
    if len(args) != len(fun_expr.type.argtypes):
        if isinstance(ast_node.func, ast.Name):
            _, function_definition_ast_node = compilation_context.symbol_table.get_symbol_definition(ast_node.func.id)
            assert function_definition_ast_node
            raise CompilationError(compilation_context, ast_node,
                                   'Argument number mismatch in function call to %s: got %s arguments, expected %s' % (
                                       ast_node.func.id, len(args), len(fun_expr.type.argtypes)),
                                   notes=[(function_definition_ast_node, 'The definition of %s was here' % ast_node.func.id)])
        else:
            raise CompilationError(compilation_context, ast_node,
                                   'Argument number mismatch in function call: got %s arguments, expected %s' % (
                                       len(args), len(fun_expr.type.argtypes)))

    for arg_index, (expr, expr_ast_node, arg_type) in enumerate(zip(args, ast_node.args, fun_expr.type.argtypes)):
        if expr.type != arg_type:
            if isinstance(ast_node.func, ast.Name):
                _, function_definition_ast_node = compilation_context.symbol_table.get_symbol_definition(ast_node.func.id)
                if isinstance(expr_ast_node, ast.Name):
                    _, var_ref_ast_node = compilation_context.symbol_table.get_symbol_definition(expr_ast_node.id)
                    raise CompilationError(compilation_context, expr_ast_node,
                                           'Type mismatch for argument %s: expected type %s but was: %s' % (
                                               arg_index, str(arg_type), str(expr.type)),
                                           notes=[
                                               (function_definition_ast_node, 'The definition of %s was here' % ast_node.func.id),
                                               (var_ref_ast_node, 'The definition of %s was here' % expr_ast_node.id),
                                           ])
                else:
                    raise CompilationError(compilation_context, expr_ast_node,
                                           'Type mismatch for argument %s: expected type %s but was: %s' % (
                                               arg_index, str(arg_type), str(expr.type)),
                                           notes=[(function_definition_ast_node, 'The definition of %s was here' % ast_node.func.id)])
            else:
                raise CompilationError(compilation_context, expr_ast_node,
                                       'Type mismatch for argument %s: expected type %s but was: %s' % (
                                           arg_index, str(arg_type), str(expr.type)))

    return ir.FunctionCall(fun_expr=fun_expr, args=args)

def var_reference_ast_to_ir(ast_node: ast.Name, compilation_context: CompilationContext):
    assert isinstance(ast_node.ctx, ast.Load)
    symbol, _ = compilation_context.symbol_table.get_symbol_definition(ast_node.id)
    if not symbol:
        raise CompilationError(compilation_context, ast_node, 'Reference to undefined variable/function: ' + ast_node.id)
    return ir.VarReference(type=symbol.type, name=symbol.name)

def list_expression_ast_to_ir(ast_node: ast.List, compilation_context: CompilationContext):
    elem_exprs = [expression_ast_to_ir(elem_expr_node, compilation_context) for elem_expr_node in ast_node.elts]
    if len(elem_exprs) == 0:
        # TODO: add support for empty lists.
        raise CompilationError(compilation_context, ast_node, 'Empty lists are not currently supported.')
    elem_type = elem_exprs[0].type
    list_type = types.ListType(elem_type)
    for elem_expr, elem_expr_ast_node in zip(elem_exprs, ast_node.elts):
        if elem_expr.type != elem_type:
            raise CompilationError(compilation_context, elem_expr_ast_node,
                                   'Found different types in list elements, this is not supported. The type of this element was %s instead of %s' % (
                                       str(elem_type), str(elem_expr.type)),
                                   notes=[(ast_node.elts[0], 'A previous list element with type %s was here.' % str(elem_type))])
    if isinstance(elem_type, types.FunctionType):
        raise CompilationError(compilation_context, ast_node,
                               'Creating lists of functions is not supported. The elements of this list have type: %s' % str(type(elem_type)))

    return ir.ListExpr(type=list_type, elem_exprs=elem_exprs)

def type_declaration_ast_to_ir_expression_type(ast_node: ast.AST, compilation_context: CompilationContext):
    if isinstance(ast_node, ast.Name) and isinstance(ast_node.ctx, ast.Load):
        if ast_node.id == 'bool':
            return types.BoolType()
        elif ast_node.id == 'Type':
            return types.TypeType()
        else:
            raise CompilationError(compilation_context, ast_node, 'Unsupported type constant: ' + ast_node.id)

    if (isinstance(ast_node, ast.Subscript)
          and isinstance(ast_node.value, ast.Name)
          and isinstance(ast_node.value.ctx, ast.Load)
          and isinstance(ast_node.ctx, ast.Load)
          and isinstance(ast_node.slice, ast.Index)):
        if ast_node.value.id == 'List':
            return types.ListType(type_declaration_ast_to_ir_expression_type(ast_node.slice.value, compilation_context))
        elif (ast_node.value.id == 'Callable'
              and isinstance(ast_node.slice.value, ast.Tuple)
              and len(ast_node.slice.value.elts) == 2
              and isinstance(ast_node.slice.value.elts[0], ast.List)
              and isinstance(ast_node.slice.value.elts[0].ctx, ast.Load)
              and all(isinstance(elem, ast.Name) and isinstance(elem.ctx, ast.Load)
                      for elem in ast_node.slice.value.elts[0].elts)):
            return types.FunctionType(
                argtypes=[type_declaration_ast_to_ir_expression_type(arg_type_decl, compilation_context)
                          for arg_type_decl in ast_node.slice.value.elts[0].elts],
                returns=type_declaration_ast_to_ir_expression_type(ast_node.slice.value.elts[1], compilation_context))

    raise CompilationError(compilation_context, ast_node, 'Unsupported type declaration.')
