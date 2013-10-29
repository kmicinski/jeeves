# macro_module.py
from macropy.core.macros import *
from macropy.core.quotes import macros, q, ast, u
from ast import *
import copy

macros = Macros()

# Returns a list of the vars assigned to in an arguments node
def get_params_in_arguments(node):
  @Walker
  def get_params(tree, collect, **kw):
    if isinstance(tree, Name):
      collect(tree.id)
  _, p1 = get_params.recurse_collect(node.args)
  _, p2 = get_params.recurse_collect(node.vararg)
  _, p3 = get_params.recurse_collect(node.kwarg)
  return p1 + p2 + p3

# Takes a FunctionDef node and returns a pair
# (list of local variables, list of parameter variables)
def get_vars_in_scope(node):
  @Walker
  def get_vars(tree, collect, stop, **kw):
    if isinstance(tree, Name) and isinstance(tree.ctx, Store):
      collect(tree.id)
    if isinstance(tree, ClassDef):
      stop()
    if tree != node and isinstance(tree, FunctionDef):
      collect(tree.name)
      stop()
    if isinstance(tree, arguments):
      pass

  @Walker
  def get_globals(tree, collect, stop, **kw):
    if isinstance(tree, Global):
      for name in tree.names:
        collect(name)
    if tree != node and (isinstance(tree, ClassDef) or isinstance(tree, FunctionDef)):
      stop()

  _, v = get_vars.recurse_collect(node)
  _, g = get_globals.recurse_collect(node)
  p = get_params_in_arguments(node.args)
  return (list(set(v) - set(g)), p)


@macros.decorator
def jeeves(tree, gen_sym, **kw):

  top_node = tree

  # ctx is a mapping from variable names to the namespace
  @Walker
  def transform(tree, stop, ctx, set_ctx, **kw):
    # not expr
    # JeevesLib.jnot(expr)
    if isinstance(tree, UnaryOp) and isinstance(tree.op, Not):
      return q[ JeevesLib.jnot(ast[tree.operand]) ]

    # a1 and a2 and ... and an
    # JeevesLib.jand(lambda : left, lambda : right)
    if isinstance(tree, BoolOp):
      if isinstance(tree.op, And):
        fn = q[ JeevesLib.jand ]
      else:
        fn = q[ JeevesLib.jor ]
      result = tree.values[-1]
      for operand in tree.values[-2::-1]:
        result = q[ JeevesLib.jand(lambda : ast[operand], lambda : ast[result]) ]
      return result

    # thn if cond else els
    # JeevesLib.jif(cond, lambda : thn, lambda : els)
    if isinstance(tree, IfExp):
      return q[ JeevesLib.jif(ast[tree.test], lambda : ast[tree.body], lambda : ast[tree.orelse]) ]

    # [expr for args in iterator]
    # JeevesLib.jmap(iterator
    if isinstance(tree, ListComp):
      elt = tree.elt
      generators = tree.generators
      assert len(generators) == 1
      assert len(generators[0].ifs) == 0
      @Walker
      def toParam(tree, **kw):
        if isinstance(tree, Store):
          return Param()
      target = toParam.recurse(generators[0].target)
      iter = generators[0].iter
      lmbda = Lambda(
        args=arguments(
          args=[target],
          vararg=None,
          kwarg=None,
          defaults=[]
        ),
        body=elt
      )
      return q[ JeevesLib.jmap(ast[iter], ast[lmbda]) ]

    # a = b
    # a = JeevesLib.jassign(a, b)
    if isinstance(tree, Assign) or isinstance(tree, AugAssign):
      # TODO handle multiple assignments case later
      # TODO handle cases where the left-hand side isn't so simple

      @Walker
      def pullExprs(tree, collect, **kw):
        if isinstance(tree, Attribute):
          if not isinstance(tree.value, Name):
            valueName = gen_sym()
            collect(Assign(
              targets=[Name(id=valueName, ctx=Store())],
              value=tree.value
            ))
            return Attribute(
              value=Name(id=valueName, ctx=Load()),
              attr=tree.attr,
              ctx=Store()
            )
        if isinstance(tree, Subscript):
          assert isinstance(tree.slice, Index)
          a = gen_sym()
          b = gen_sym()
          collect(Assign(
            targets=[Name(id=a, ctx=Store())],
            value=tree.value
          ))
          collect(Assign(
            targets=[Name(id=b, ctx=Store())],
            value=tree.slice.value
          ))
          return Subscript(
            value=Name(id=a,ctx=Load()),
            slice=Index(Name(id=b,ctx=Load())),
            ctx=Store()
          )
      
      @Walker
      def makeLoad(tree, **kw):
        if isinstance(tree, Store):
          return Load()

      @Walker
      def makeUnassigned(tree, stop, **kw):
        if isinstance(tree, Attribute):
          stop()
          return q[ JeevesLib.jgetattr(ast[tree.value], u[tree.attr]) ]
        if isinstance(tree, Subscript):
          stop()
          return q[ JeevesLib.jgetitem(ast[tree.value], ast[tree.slice.value]) ]

      if isinstance(tree, Assign):
        assert len(tree.targets) == 1
        target = tree.targets[0]
      else:
        target = tree.target
      target = transform.recurse(target, ctx=ctx)
      value = transform.recurse(tree.value, ctx=ctx)
      stop()

      newStore, prevStmts = pullExprs.recurse_collect(target)
      exprLoad1 = makeLoad.recurse(copy.deepcopy(newStore))
      exprLoad = makeUnassigned.recurse(exprLoad1)

      if isinstance(tree, Assign):
        newvalue = value
      else:
        newvalue = BinOp(exprLoad, tree.op, value)

      result = prevStmts + [copy_location(
        Assign([newStore], q[ JeevesLib.jassign(ast[exprLoad], ast[newvalue]) ]),
        tree
      )]
      return result

    # If a1,a2,..,an are all the local variables, change
    #
    # if condition:
    #     thn_body
    # else:
    #     els_body
    # 
    # to
    #
    # def thn_fn_name():
    #     thn_body
    # def els_fn_name():
    #     els_body
    # jif(condition, thn_fn_name, els_fn_name)
    if isinstance(tree, If):
      # TODO search over the bodies, and only do this for the variables that
      # get assigned to.
      localvars = ctx
      
      thn_fn_name = gen_sym()
      els_fn_name = gen_sym()

      test = transform.recurse(tree.test, ctx=ctx)
      thn_body = transform.recurse(tree.body, ctx=ctx)
      els_body = transform.recurse(tree.orelse, ctx=ctx)
      stop()

      def get_func(funcname, funcbody):
        return FunctionDef(
          name=funcname, 
          args=arguments(
            args=[],
            vararg=None,
            kwarg=None,
            defaults=[],
          ),
          body=funcbody or [Pass()],
          decorator_list=[],
        )

      return [
        get_func(thn_fn_name, thn_body),
        get_func(els_fn_name, els_body),
        Expr(value=q[
          JeevesLib.jif(ast[test],
            ast[Name(id=thn_fn_name,ctx=Load())],
            ast[Name(id=els_fn_name,ctx=Load())],
          )
        ])
      ]

    if isinstance(tree, Compare):
      assert len(tree.ops) == 1
      # TODO other comparisons besides 'in'
      if isinstance(tree.ops[0], In):
        return q[ JeevesLib.jhas(ast[tree.comparators[0]], ast[tree.left]) ]

    if isinstance(tree, FunctionDef):
      varNames, paramNames = get_vars_in_scope(tree)
      namespaceName = gen_sym()

      # namespaceName = Namespace(param1=value1,...)
      namespaceStmt = Assign(
        targets=[Name(id=namespaceName,ctx=Store())],
        value=Call(
          func=q[JeevesLib.Namespace],
          args=[Dict(
            keys=[Str(p) for p in paramNames],
            values=[Name(id=p, ctx=Load()) for p in paramNames],
          )],
          keywords=[],
          starargs=None,
          kwargs=None,
        )
      )

      # make a copy of the scope mapping nad update it
      scopeMapping = dict(ctx)
      for name in varNames + paramNames:
        scopeMapping[name] = namespaceName

      name = tree.name
      args = transform.recurse(tree.args, ctx=ctx) 
      body = transform.recurse(tree.body, ctx=scopeMapping)
      decorator_list = transform.recurse(tree.decorator_list, ctx=ctx)
      newtree = copy_location(
        FunctionDef(name=name, args=args,
                body=[namespaceStmt]+body,
                decorator_list=decorator_list),
        tree
      )

      stop()
      
      if tree == top_node:
        return newtree

      outerAssignStmt = copy_location(Assign(
        targets=[Attribute(
          value=Name(id=ctx[tree.name], ctx=Load()),
          attr=tree.name,
          ctx=Store()
        )],
        value=Name(id=tree.name, ctx=Load()),
      ), tree)
        
      return [newtree, outerAssignStmt]

    if isinstance(tree, Lambda):
      paramNames = get_params_in_arguments(tree.args)

      # make a copy of the scope mapping and update it
      scopeMapping = dict(ctx)
      for name in paramNames:
        scopeMapping[name] = None

      args = transform.recurse(tree.args, ctx=ctx)
      body = transform.recurse(tree.body, ctx=scopeMapping)
      newlambda = copy_location(Lambda(args=args, body=body), tree)
      stop()
      return newlambda

    if isinstance(tree, Name) and (isinstance(tree.ctx, Load) or isinstance(tree.ctx, Store) or isinstance(tree.ctx, Del)):
      if tree.id in ctx and ctx[tree.id] != None:
        return Attribute(
          value=Name(id=ctx[tree.id], ctx=Load()),
          attr=tree.id,
          ctx=tree.ctx
        )

    if isinstance(tree, arguments):
      stop()
      return arguments(
        args=tree.args,
        vararg=tree.vararg,
        kwarg=tree.kwarg,
        defaults=transform.recurse(tree.defaults, ctx=ctx)
      )

  result = transform.recurse(tree, ctx={})
  return result
