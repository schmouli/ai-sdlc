"""AST-based parser: file/symbol discovery, import and call-edge extraction.

Implements research R3/R4: stdlib `ast` walk producing a ParseResult per file
(imports incl. function-scoped + relative resolution; defs with qualified
names; calls classified into Tier A 'name' / Tier B 'method'); per-file
NameResolutionTable consumed for call resolution. Unresolvable calls produce
no edge (no hallucinated structure). Failures carry reason codes
KG-PARSE-SYNTAX / KG-PARSE-ENCODING / KG-PARSE-EMPTY with file+line detail;
a parse failure never corrupts previously valid state for the file (the
caller's skip path simply keeps prior snapshot data untouched).
"""
import ast
import os

from src.kg import report as kg_report

# Module-level aliases (tests + callers use parser.KG_* directly).
KG_PARSE_SYNTAX = kg_report.KG_PARSE_SYNTAX
KG_PARSE_ENCODING = kg_report.KG_PARSE_ENCODING
KG_PARSE_EMPTY = kg_report.KG_PARSE_EMPTY


class ImportRec:
    __slots__ = ("module", "names", "is_relative", "resolved_target", "scope", "line")

    def __init__(self, module, names, is_relative, resolved_target, scope, line):
        self.module = module
        self.names = names
        self.is_relative = is_relative
        self.resolved_target = resolved_target
        self.scope = scope
        self.line = line


class DefRec:
    __slots__ = ("qualified_name", "kind", "line", "end_line", "doc_head")

    def __init__(self, qualified_name, kind, line, end_line, doc_head=""):
        self.qualified_name = qualified_name
        self.kind = kind
        self.line = line
        self.end_line = end_line
        self.doc_head = doc_head


class CallRec:
    __slots__ = ("name", "context_scope", "resolved_target", "tier")

    def __init__(self, name, context_scope, resolved_target=None, tier="name"):
        self.name = name
        self.context_scope = context_scope
        self.resolved_target = resolved_target
        self.tier = tier


class ParseResult:
    __slots__ = ("path", "imports", "defs", "calls", "failures")

    def __init__(self, path, imports, defs, calls, failures):
        self.path = path
        self.imports = imports
        self.defs = defs
        self.calls = calls
        self.failures = failures


def _first_doc_line(node):
    doc = ast.get_docstring(node)
    return doc.strip().split("\n")[0] if doc else ""


def _def_kind(node):
    if isinstance(node, ast.FunctionDef):
        return "function"
    if isinstance(node, ast.AsyncFunctionDef):
        return "function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "assignment"


def _walk_defs(tree):
    """Yield (qualified_name, node, kind) for module-level defs (1 level of class nesting)."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node, _def_kind(node)
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield f"{node.name}.{sub.name}", sub, "method"


def _module_assignments(tree):
    names = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def resolve_module(module, is_relative, level, from_file, repo_root="."):
    """Map a module path (relative or absolute) to a repo-relative .py path.

    Returns the in-repo file when a candidate exists under repo_root; returns
    the dotted module name unchanged for external modules (kept as traversal
    context, flagged `external` by the caller). Repo layout: package dirs
    contain __init__.py.
    """
    module = (module or "").strip()
    if is_relative:
        base_parts = from_file.replace("\\", "/").split("/")[:-1]
        for _ in range(max(0, level - 1)):
            if base_parts:
                base_parts.pop()
        parts = base_parts + (module.split(".") if module else [])
        if not parts:
            return None
        candidates = ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]
        for cand in candidates:
            if os.path.exists(os.path.join(repo_root, cand)):
                return cand
        return "/".join(parts) + ".py"
    parts = module.split(".")
    candidates = ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]
    for cand in candidates:
        if os.path.exists(os.path.join(repo_root, cand)):
            return cand
    return module or None


class NameResolutionTable:
    """Per-file alias -> (repo-relative module target, symbol|module)."""

    def __init__(self, importer_path, imports):
        self.importer_path = importer_path
        self.alias_map = {}
        self.modules = {}
        for imp in imports:
            if imp.is_relative or self._maybe_local(imp.module or ""):
                if imp.module and not imp.names:
                    continue
                if imp.module:  # from X import f as g
                    for name, asname in imp.names:
                        self.alias_map[asname or name] = (imp.resolved_target, name)
                else:  # import X [as y] handled below via module branch
                    pass
            if not imp.module:  # plain `import a.b[.c] [as y]` statement
                for name, asname in imp.names:
                    top_as = asname or name.split(".")[-1]
                    self.modules[top_as] = imp.resolved_target

    def _maybe_local(self, module):
        return module.startswith(("src.", "scripts.", "utils.", "config.", "tests."))

    def resolve_name(self, name):
        """Tier A: alias is an imported symbol -> (file, qualified symbol)."""
        return self.alias_map.get(name)

    def resolve_module_attr(self, root):
        """Module-qualified access: root module alias -> repo file."""
        return self.modules.get(root)


def parse_source(source, rel_path, repo_root="."):
    """Parse one Python file into a ParseResult.

    rel_path: repo-relative posix path. repo_root used for existence checks
    when resolving import targets.
    """
    rel_path = rel_path.replace("\\", "/")
    failures = []
    imports, defs, calls = [], [], []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        failures.append(kg_report.emit_failure(
            kg_report.KG_PARSE_SYNTAX,
            f"{rel_path}:{exc.lineno}: {exc.msg} (skipping extraction; prior state preserved)",
        ))
        return ParseResult(rel_path, imports, defs, calls, failures)
    except UnicodeDecodeError as exc:
        failures.append(kg_report.emit_failure(
            kg_report.KG_PARSE_ENCODING, f"{rel_path}: {exc}"
        ))
        return ParseResult(rel_path, imports, defs, calls, failures)

    if not source.strip():
        failures.append(kg_report.emit_failure(
            kg_report.KG_PARSE_EMPTY, f"{rel_path}: no defs (pure script/module marker)"
        ))
        return ParseResult(rel_path, imports, defs, calls, failures)

    # --- imports (module-level and function-scoped) ---
    module_scope_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = resolve_module(alias.name, False, 0, rel_path, repo_root)
                rec = ImportRec(alias.name, [(alias.name, alias.asname)], False, target,
                                "module", node.lineno)
                imports.append(rec)
                module_scope_imports.append(rec)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            target = resolve_module(module, bool(node.level), node.level, rel_path, repo_root)
            rec = ImportRec(
                module, [(alias.name, alias.asname) for alias in node.names],
                bool(node.level), target, "module", node.lineno,
            )
            imports.append(rec)
            module_scope_imports.append(rec)

    # function-scoped imports: walk with scope tracking
    class ImportScopeVisitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = "module"

        def visit_FunctionDef(self, node):
            prev, self.scope = self.scope, "function"
            self.generic_visit(node)
            self.scope = prev

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            prev, self.scope = self.scope, "class"
            self.generic_visit(node)
            self.scope = prev

        def visit_Import(self, node):
            if self.scope != "module":
                for alias in node.names:
                    target = resolve_module(alias.name, False, 0, rel_path, repo_root)
                    imports.append(ImportRec(alias.name, [], False, target, self.scope, node.lineno))
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if self.scope != "module":
                module = node.module or ""
                target = resolve_module(module, bool(node.level), node.level, rel_path, repo_root)
                imports.append(
                    ImportRec(module, list(node.names), bool(node.level), target, self.scope,
                              node.lineno)
                )
            self.generic_visit(node)

    ImportScopeVisitor().visit(tree)

    table = NameResolutionTable(rel_path, module_scope_imports)

    # --- defs ---
    for qualified_name, node, kind in _walk_defs(tree):
        defs.append(DefRec(qualified_name, kind, node.lineno, node.end_lineno or node.lineno,
                           _first_doc_line(node)))
    for name in _module_assignments(tree):
        defs.append(DefRec(name, "assignment", 0, 0, ""))

    # --- calls: tier classification via a scoped assignment map (Tier B 1-hop) ---
    local_syms = {d.qualified_name for d in defs} | {
        d.qualified_name.split(".")[0] for d in defs if d.kind == "class"
    }
    method_origins = set(local_syms)

    class CallVisitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = "module"
            self.assign_types = {}

        def visit_FunctionDef(self, node):
            prev, self.scope = self.scope, node.name
            self.generic_visit(node)
            self.scope = prev

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            # 1-hop origin tracking: x = Widget() / x = imported_factory()
            root = None
            if isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name):
                    resolved = table.resolve_name(func.id)
                    if resolved is not None:
                        root = resolved
                    elif func.id in method_origins:
                        root = (rel_path, func.id)
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    module_file = table.resolve_module_attr(func.value.id)
                    if module_file is not None:
                        root = (module_file, func.attr)
            if root is not None:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.assign_types[t.id] = root
            self.generic_visit(node)

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Name):
                resolved = table.resolve_name(func.id)
                if resolved is not None:
                    calls.append(CallRec(func.id, self.scope, resolved, "name"))
                elif func.id in method_origins:
                    calls.append(CallRec(func.id, self.scope, (rel_path, func.id), "name"))
                # otherwise: builtin/dynamic/direct-local-def call -> no cross edge
                # (a call to a same-file def is represented by that def existing;
                #  cross-file structure is what edges convey - research R4)
            elif isinstance(func, ast.Attribute):
                root_name = func.value.id if isinstance(func.value, ast.Name) else None
                if root_name is not None:
                    origin = self.assign_types.get(root_name)
                    if origin is not None:
                        calls.append(CallRec(func.attr, self.scope, origin, "method"))
                    else:
                        module_file = table.resolve_module_attr(root_name)
                        if module_file is not None:
                            calls.append(CallRec(func.attr, self.scope, (module_file, func.attr), "name"))
            self.generic_visit(node)

    CallVisitor().visit(tree)
    return ParseResult(rel_path, imports, defs, calls, failures)
