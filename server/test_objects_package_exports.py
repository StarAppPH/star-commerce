"""Guards the re-export list in objects/__init__.py.

Callers import every model from `objects`, and AppCreator generates a
`<Name>Schema.ts` only for classes the package exposes with __module__
'objects' (objects/__init__.py stamps it on what it re-exports). A model
defined in a submodule but never re-exported keeps __module__
'objects.<module>', so it silently gets no schema of its own. These tests fail
instead.
"""
import ast
import importlib
import pkgutil
from pathlib import Path

import objects


def _submodules():
    """Every module in the package, imported, including one that nothing
    imports yet."""
    return [
        importlib.import_module(f'objects.{info.name}')
        for info in pkgutil.iter_modules(objects.__path__)
    ]


def _top_level_names(module) -> list[str]:
    """The names the module's source defines at the top level: classes,
    functions and assigned constants (imports excluded)."""
    tree = ast.parse(Path(module.__file__).read_bytes())
    names = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets
                         if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name):
            names.append(node.target.id)
    return names


def test_everySubmoduleDefinition_isReExportedFromObjects():
    submodules = _submodules()
    assert submodules, 'objects/ has no submodules'
    missing = [
        f'{module.__name__}.{name}: re-export {name} from objects/__init__.py'
        for module in submodules for name in _top_level_names(module)
        if getattr(objects, name, None) is not getattr(module, name)
    ]
    assert not missing, ('Defined in an objects submodule but not re-exported '
                         'by the package:\n' + '\n'.join(missing))


def test_noClassReachableFromThePackage_reportsASubmodule():
    namespaces = [objects] + _submodules()
    unstamped = sorted({
        f'{value.__module__}.{value.__qualname__}: re-export '
        f'{value.__name__} from objects/__init__.py'
        for namespace in namespaces for value in vars(namespace).values()
        if isinstance(value, type) and value.__module__.startswith('objects.')
    })
    assert not unstamped, (
        "Classes whose __module__ is still 'objects.<module>', so AppCreator "
        'generates no TS schema for them:\n' + '\n'.join(unstamped))
