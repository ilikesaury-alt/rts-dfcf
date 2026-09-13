import ast
import subprocess

# 1) 原始 config.py（git HEAD 版本）中定义的名字 + 其 re-export 的名字
old_src = subprocess.check_output(
    ["git", "show", "HEAD:scanner/config.py"], text=True
)
old_tree = ast.parse(old_src)
old_names = set()
for node in old_tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                old_names.add(t.id)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        old_names.add(node.name)
old_reexports = set()
for node in old_tree.body:
    if isinstance(node, ast.ImportFrom) and node.module in (
        "scanner.holidays",
        "scanner.weights",
        "scanner.categories",
    ):
        for a in node.names:
            old_reexports.add(a.asname or a.name)

# 2) 新版 scanner.config 实际暴露的名字
import scanner.config as c  # noqa: E402

old_api = old_names | old_reexports
missing = sorted(n for n in old_api if not hasattr(c, n))
print("MISSING (old API not re-exported):")
for n in missing:
    print("  ", n)
print("total missing:", len(missing))
