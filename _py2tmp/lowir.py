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

import typing

import itertools

import _py2tmp.types as types
from typing import List, Set, Optional

class CodegenError(Exception):
    pass

class Expr:
    def __init__(self, type: types.ExprType):
        self.type = type

    def to_cpp(self) -> str: ... # pragma: no cover

    def references_any_of(self, variables: Set[str]) -> bool: ... # pragma: no cover

class StaticAssert:
    def __init__(self, expr: Expr, message: str):
        assert expr.type.kind == types.ExprKind.BOOL
        self.expr = expr
        self.message = message

    def to_cpp(self, enclosing_function_defn_args, enclosing_function_defn_name):
        assert (enclosing_function_defn_args is None) == (enclosing_function_defn_name is None)
        if enclosing_function_defn_args:
            bound_variables = {_identifier_to_cpp(type=arg_decl.type, name=arg_decl.name)
                               for arg_decl in enclosing_function_defn_args}
            assert bound_variables
        if not enclosing_function_defn_args or self.expr.references_any_of(bound_variables):
            return 'static_assert({cpp_meta_expr}, "{message}");'.format(
                cpp_meta_expr=self.expr.to_cpp(),
                message=self.message)
        else:
            # The expression is constant, we need to add a reference to a variable bound in this function to prevent the
            # static_assert from being evaluated before the template is instantiated.
            for arg_decl in enclosing_function_defn_args:
                if arg_decl.type.kind == types.ExprKind.BOOL:
                    return 'static_assert(AlwaysTrueFromBool<{bound_var}>::value && {cpp_meta_expr}, "{message}");'.format(
                        bound_var=_identifier_to_cpp(type=arg_decl.type, name=arg_decl.name),
                        cpp_meta_expr=self.expr.to_cpp(),
                        message=self.message)
                elif arg_decl.type.kind == types.ExprKind.TYPE:
                    return 'static_assert(AlwaysTrueFromType<{bound_var}>::value && {cpp_meta_expr}, "{message}");'.format(
                        bound_var=_identifier_to_cpp(type=arg_decl.type, name=arg_decl.name),
                        cpp_meta_expr=self.expr.to_cpp(),
                        message=self.message)
            raise CodegenError('Unable to convert to C++ an assertion in the function {function_name} because it\'s '
                               'a constant expression and {function_name} only has functions as params. '
                               'You should move the assertion outside the function or reference one of {function_name}\'s params in the assertion.'.format(function_name=enclosing_function_defn_name))

def _generate_identifiers(prefix):
    x = 0
    while True:
        yield prefix + str(x)
        x += 1

class Assignment:
    def __init__(self, lhs: 'VarReference', rhs: Expr):
        assert lhs.type == rhs.type
        self.lhs = lhs
        self.rhs = rhs

    def to_cpp(self, enclosing_function_defn=None):
        lhs_cpp = self.lhs.to_cpp()
        return Assignment.to_cpp_internal(
            self.lhs.type,
            lhs_cpp,
            self.rhs,
            _generate_identifiers(prefix=lhs_cpp + '_Helper'),
            _generate_identifiers(prefix='Param_'))

    @staticmethod
    def to_cpp_internal(type: types.ExprType, lhs_cxx_name: str, rhs: Expr, helper_identifier_generator, param_identifier_generator):
        if type.kind == types.ExprKind.BOOL:
            cpp_meta_expr = rhs.to_cpp()
            return '''\
                static constexpr bool {lhs_cxx_name} = {cpp_meta_expr};
                '''.format(**locals())
        elif type.kind == types.ExprKind.TYPE:
            cpp_meta_expr = rhs.to_cpp()
            return '''\
                using {lhs_cxx_name} = {cpp_meta_expr};
                '''.format(**locals())
        elif type.kind == types.ExprKind.TEMPLATE:
            assert isinstance(type, types.FunctionType)
            return_type = type.returns

            template_args = [(next(param_identifier_generator), arg_type)
                             for arg_type in type.argtypes]
            template_args_decl = ', '.join(_type_to_template_param_declaration(arg_type) + ' ' + template_arg_name
                                           for template_arg_name, arg_type in template_args)
            inner_lhs_cxx_name = {
                types.ExprKind.BOOL: 'value',
                types.ExprKind.TYPE: 'type',
                types.ExprKind.TEMPLATE: 'type',
            }[return_type.kind]
            cpp_inner_assignment = Assignment.to_cpp_internal(type=return_type,
                                                              lhs_cxx_name=inner_lhs_cxx_name,
                                                              rhs=FunctionCall(rhs,
                                                                               args=[VarReference(type=arg_type, cxx_name=template_arg_name)
                                                                                     for template_arg_name, arg_type in template_args]),
                                                              helper_identifier_generator=helper_identifier_generator,
                                                              param_identifier_generator=param_identifier_generator)
            if lhs_cxx_name == 'type':
                # We need special handling for this case, because we can't generate code like:
                # struct type {
                #   using type = int;
                # };
                helper_cxx_name = next(helper_identifier_generator)
                template_args_use = ', '.join(template_arg_name
                                              for template_arg_name, arg_type in template_args)
                return '''\
                    template <{template_args_decl}>
                    struct {helper_cxx_name} {{
                      {cpp_inner_assignment}
                    }};
                    template <{template_args_decl}>
                    using type = {helper_cxx_name}<{template_args_use}>;
                    '''.format(**locals())
            else:
                return '''\
                    template <{template_args_decl}>
                    struct {lhs_cxx_name} {{
                      {cpp_inner_assignment}
                    }};
                    '''.format(**locals())
        else:
            raise NotImplementedError('Unexpected expression type kind: %s' % type.kind)

def _type_to_template_param_declaration(type):
    if type.kind == types.ExprKind.BOOL:
        return 'bool'
    elif type.kind == types.ExprKind.TYPE:
        return 'typename'
    elif type.kind == types.ExprKind.TEMPLATE:
        return ('template <'
                + ', '.join(_type_to_template_param_declaration(arg_type)
                            for arg_type in type.argtypes)
                + '> class')
    else:
        raise NotImplementedError('Unsupported argument kind: ' + str(type.kind))

class FunctionArgDecl:
    def __init__(self, type: types.ExprType, name: str = ''):
        self.type = type
        self.name = name

    def to_cpp(self):
        return _identifier_to_cpp(self.type, self.name)

def _identifier_to_cpp(type, name):
    if type.kind == types.ExprKind.BOOL:
        return name
    elif type.kind in (types.ExprKind.TYPE, types.ExprKind.TEMPLATE):
        return name.title().replace("_", "")
    else:
        raise NotImplementedError('Unsupported kind: ' + str(type.kind))

class FunctionSpecialization:
    def __init__(self,
                 args: List[FunctionArgDecl],
                 patterns: 'Optional[List[TypePatternLiteral]]',
                 asserts_and_assignments: List[typing.Union[StaticAssert, Assignment]],
                 expression: Expr):
        self.args = args
        self.expr = expression
        # patterns==None means that this is not actually a specialization (it's the main definition).
        # Instead, patterns==[] means that this is a full specialization.
        if patterns is None:
            self.patterns = None
        else:
            patterns_set = set(pattern_literal.cxx_pattern
                               for pattern_literal in patterns)
            args_set = {_identifier_to_cpp(type=arg.type, name=arg.name)
                        for arg in args}
            if args_set == patterns_set:
                # This specializes nothing, so it's the main definition. Keeping the explicit patterns would cause
                # C++ compilation errors, so we remove them.
                self.patterns = None
            else:
                self.patterns = patterns

        return_assignment_cxx_name = {
            types.ExprKind.BOOL: 'value',
            types.ExprKind.TYPE: 'type',
            types.ExprKind.TEMPLATE: 'type',
        }[self.expr.type.kind]

        return_assignment = Assignment(lhs=VarReference(type=self.expr.type, cxx_name=return_assignment_cxx_name),
                                       rhs=self.expr)
        self.asserts_and_assignments = asserts_and_assignments + [return_assignment]
        self.type = types.FunctionType(argtypes=[arg.type for arg in args], returns=expression.type)

    def is_main_definition(self):
        return self.patterns is None

    def to_cpp(self, original_name, cxx_name):
        asserts_and_assignments_str = ''.join(x.to_cpp(enclosing_function_defn_args=self.args, enclosing_function_defn_name=original_name) + '\n'
                                              if isinstance(x, StaticAssert)
                                              else x.to_cpp() + '\n'
                                              for x in self.asserts_and_assignments)
        template_args = ', '.join(_type_to_template_param_declaration(arg.type) + ' ' + _identifier_to_cpp(type=arg.type, name=arg.name)
                                  for arg in self.args)
        if self.patterns is not None:
            patterns_str = ', '.join(pattern.to_cpp()
                                     for pattern in self.patterns)
            return '''\
                template <{template_args}>
                struct {cxx_name}<{patterns_str}> {{
                  {asserts_and_assignments_str}  
                }};
                '''.format(**locals())
        else:
            return '''\
                template <{template_args}>
                struct {cxx_name} {{
                  {asserts_and_assignments_str}  
                }};
                '''.format(**locals())

class FunctionDefn:
    def __init__(self,
                 return_type: types.ExprType,
                 args: List[FunctionArgDecl],
                 main_definition: Optional[FunctionSpecialization],
                 specializations: List[FunctionSpecialization],
                 name: Optional[str] = None,
                 original_name: Optional[str] = None,
                 cxx_name: Optional[str] = None):
        assert name or cxx_name
        assert not (name and cxx_name)
        assert (name is None) != (original_name is None)
        assert main_definition or specializations
        assert not main_definition or main_definition.patterns is None
        self.type = types.FunctionType(argtypes=[arg.type for arg in args], returns=return_type)
        self.name = name
        self.cxx_name = cxx_name
        self.original_name = original_name or name
        self.args = args
        self.main_definition = main_definition
        self.specializations = specializations

    def to_cpp(self):
        if self.name:
            metafunction_name = _identifier_to_cpp(type=self.type, name=self.name)
        else:
            metafunction_name = self.cxx_name
        if self.main_definition:
            main_definition_str = self.main_definition.to_cpp(original_name=self.original_name, cxx_name=metafunction_name)
        else:
            template_args = ', '.join(_type_to_template_param_declaration(arg.type) + ' ' + _identifier_to_cpp(type=arg.type, name=arg.name)
                                      for arg in self.args)
            main_definition_str = '''\
                template <{template_args}>
                struct {metafunction_name};
                '''.format(**locals())
        specializations_str = ''.join(specialization.to_cpp(original_name=self.original_name, cxx_name=metafunction_name)
                                      for specialization in self.specializations)
        return main_definition_str + specializations_str

class Literal(Expr):
    def __init__(self, value, type):
        super().__init__(type)
        assert value in (True, False)
        self.value = value

    def references_any_of(self, variables: Set[str]):
        return False

    def to_cpp(self):
        return {
            True: 'true',
            False: 'false',
        }[self.value]

class TypeLiteral(Expr):
    def __init__(self, cpp_type: str):
        super().__init__(type=types.TypeType())
        self.cpp_type = cpp_type

    def references_any_of(self, variables: Set[str]):
        return False

    def to_cpp(self):
        return self.cpp_type

class TypePatternLiteral:
    def __init__(self,
                 type_name: str = None,
                 cxx_pattern: str = None):
        assert (type_name is None) != (cxx_pattern is None)
        if cxx_pattern:
            self.cxx_pattern = cxx_pattern
        else:
            self.cxx_pattern = _identifier_to_cpp(type=types.TypeType(), name=type_name)

    def to_cpp(self):
        return self.cxx_pattern

class EqualityComparison(Expr):
    def __init__(self, lhs: Expr, rhs: Expr):
        super().__init__(type=types.BoolType())
        assert lhs.type == rhs.type
        assert lhs.type.kind in (types.ExprKind.BOOL, types.ExprKind.TYPE)
        self.lhs = lhs
        self.rhs = rhs

    def references_any_of(self, variables: Set[str]):
        return self.lhs.references_any_of(variables) or self.rhs.references_any_of(variables)

    def to_cpp(self):
        lhs_cpp_meta_expr = self.lhs.to_cpp()
        rhs_cpp_meta_expr = self.rhs.to_cpp()
        cpp_str_template = {
            types.ExprKind.BOOL: '({lhs_cpp_meta_expr}) == ({rhs_cpp_meta_expr})',
            types.ExprKind.TYPE: 'std::is_same<{lhs_cpp_meta_expr}, {rhs_cpp_meta_expr}>::value'
        }[self.lhs.type.kind]
        return cpp_str_template.format(**locals())

class FunctionCall(Expr):
    def __init__(self, fun_expr: Expr, args: List[Expr]):
        assert fun_expr.type.kind == types.ExprKind.TEMPLATE
        super().__init__(type=fun_expr.type.returns)
        assert len(fun_expr.type.argtypes) == len(args)
        assert self.type.kind in (types.ExprKind.BOOL, types.ExprKind.TYPE, types.ExprKind.TEMPLATE)
        self.fun_expr = fun_expr
        self.args = args

    def references_any_of(self, variables: Set[str]):
        return self.fun_expr.references_any_of(variables) or any(expr.references_any_of(variables)
                                                                 for expr in self.args)

    def to_cpp(self, is_nested_call=False):
        template_params = ', '.join(arg.to_cpp() for arg in self.args)
        if isinstance(self.fun_expr, FunctionCall):
            cpp_fun = self.fun_expr.to_cpp(is_nested_call=True)
        else:
            cpp_fun = self.fun_expr.to_cpp()
        if self.type.kind == types.ExprKind.BOOL:
            assert not is_nested_call
            cpp_str_template = '{cpp_fun}<{template_params}>::value'
        elif self.type.kind in (types.ExprKind.TYPE, types.ExprKind.TEMPLATE):
            if is_nested_call:
                cpp_str_template = '{cpp_fun}<{template_params}>::template type'
            else:
                cpp_str_template = 'typename {cpp_fun}<{template_params}>::type'
        else:
            raise NotImplementedError('Type kind: %s' % self.type.kind)
        return cpp_str_template.format(**locals())

class VarReference(Expr):
    def __init__(self, type: types.ExprType, name: Optional[str] = None, cxx_name: Optional[str] = None):
        super().__init__(type=type)
        assert name or cxx_name
        assert not (name and cxx_name)
        self.name = name
        self.cxx_name = cxx_name

    def references_any_of(self, variables: Set[str]):
        if self.name:
            return _identifier_to_cpp(type=self.type, name=self.name) in variables
        else:
            assert self.cxx_name
            return self.cxx_name in variables

    def to_cpp(self):
        if self.name:
            return _identifier_to_cpp(type=self.type, name=self.name)
        else:
            assert self.cxx_name
            return self.cxx_name

class ListExpr(Expr):
    def __init__(self, type: types.ListType, elem_exprs: List[Expr]):
        super().__init__(type=type)
        assert type.elem_type.kind in (types.ExprKind.BOOL, types.ExprKind.TYPE)
        self.elem_exprs = elem_exprs

    def references_any_of(self, variables: Set[str]):
        return any(expr.references_any_of(variables) for expr in self.elem_exprs)

    def to_cpp(self):
        template_params = ', '.join(elem_expr.to_cpp()
                                    for elem_expr in self.elem_exprs)
        cpp_str_template = {
            types.ExprKind.BOOL: 'BoolList<{template_params}>',
            types.ExprKind.TYPE: 'List<{template_params}>'
        }[self.type.elem_type.kind]
        return cpp_str_template.format(**locals())

class Module:
    def __init__(self, function_defns: List[FunctionDefn], assertions: List[StaticAssert]):
        self.function_defns = function_defns
        self.assertions = assertions

    def to_cpp(self):
        includes = '''\
            #include <tmppy/tmppy.h>
            #include <type_traits>     
            '''
        return (includes
                + ''.join(x.to_cpp()
                          for x in self.function_defns)
                + ''.join(x.to_cpp(enclosing_function_defn_args=None, enclosing_function_defn_name=None)
                          for x in self.assertions))