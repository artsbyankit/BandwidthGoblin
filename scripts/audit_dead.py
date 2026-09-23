"""AST dead-code / unreferenced-symbol audit for linkboost.py (stdlib only)."""
import ast, sys, collections

SRC = sys.argv[1]
tree = ast.parse(open(SRC, encoding='utf-8').read(), SRC)

defs = {}          # name -> (lineno, kind)
reads = collections.Counter()   # name -> count of NAME loads / attribute bases
writes = set()     # names assigned at module level
mod_assign = []    # (lineno, name, value_src) for module-level assignments

# collect module-level names we must ignore (imports, globals referenced via globals())
IGNORE = {'__file__', '__name__', '__main__', 'main', 'BUILD', 'VERSION'}

class _V(ast.NodeVisitor):
    def __init__(self, scope='mod'):
        self.scope = scope
    def visit_Name(self, n):
        if isinstance(n.ctx, ast.Load):
            reads[n.id] += 1
        self.generic_visit(n)
    def visit_Attribute(self, n):
        # count base of attribute access as a read of the base name
        if isinstance(n.value, ast.Name) and isinstance(n.ctx, ast.Load):
            reads[n.value.id] += 1
        self.generic_visit(n)
    def visit_Import(self, n):
        for a in n.names:
            defs[a.asname or a.name.split('.')[0]] = (n.lineno, 'import')
    def visit_ImportFrom(self, n):
        for a in n.names:
            defs[a.asname or a.name] = (n.lineno, 'import')
    def visit_FunctionDef(self, n):
        kind = 'def/nested'
        defs.setdefault(n.name, (n.lineno, 'def/class'))
        # walk body but don't need to track nested separately for reads
        self.generic_visit(n)
    def visit_AsyncFunctionDef(self, n):
        self.generic_visit(n)
    def visit_ClassDef(self, n):
        defs.setdefault(n.name, (n.lineno, 'class'))
        self.generic_visit(n)
    def visit_Assign(self, n):
        if self.scope == 'mod':
            for t in n.targets:
                if isinstance(t, ast.Name):
                    writes.add(t.id)
        self.generic_visit(n)
    def visit_AugAssign(self, n):
        if self.scope == 'mod' and isinstance(n.target, ast.Name):
            writes.add(n.target.id)
        self.generic_visit(n)
    def visit_AnnAssign(self, n):
        if self.scope == 'mod' and isinstance(n.target, ast.Name):
            writes.add(n.target.id)
        self.generic_visit(n)

_V().visit(tree)

# also count uses inside function bodies without double counting: rerun visitor on funcs
def all_reads(node):
    c = collections.Counter()
    class W(ast.NodeVisitor):
        def visit_Name(self, n):
            if isinstance(n.ctx, ast.Load):
                c[n.id] += 1
        def visit_Attribute(self, n):
            if isinstance(n.value, ast.Name) and isinstance(n.ctx, ast.Load):
                c[n.value.id] += 1
            self.generic_visit(n)
    W().visit(node)
    return c

# functions/classes: how many reads OUTSIDE their own body?
for name, (ln, kind) in defs.items():
    if kind == 'import':
        continue
    if name in IGNORE:
        continue
    # find the defn node
    node = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name and n.lineno == ln:
            node = n
            break
    if node is None:
        continue
    # total reads in module minus reads inside the def's own body & nested defs under it
    own = all_reads(node)
    total = reads.get(name, 0)
    # the def statement itself names it once inside the ClassDef/FuncDef body as assignment
    outside = total - own.get(name, 0)
    if outside == 0:
        print(f'UNUSED-DEF {kind}: {name} (def line {ln}) - defined but never referenced')

# module-level assigns never read as a Load anywhere
for name in sorted(writes):
    if name in IGNORE or name in defs:
        continue
    if reads.get(name, 0) == 0:
        print(f'UNUSED-MODVAR: {name} (assigned at module level, never read)')

# detect module-level names that are ONLY written (set but never loaded)