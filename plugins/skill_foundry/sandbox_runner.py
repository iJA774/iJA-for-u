"""对生成脚本执行静态策略检查，并在受限子进程中运行 JSON 函数。"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import types
from collections.abc import Callable
from typing import Any

POLICY_VERSION = "ija-safe-python-v1"
MAX_SOURCE_BYTES = 32_000
MAX_INPUT_BYTES = 64_000
MAX_OUTPUT_BYTES = 64_000
MAX_AST_NODES = 3_000

SAFE_MODULE_EXPORTS: dict[str, frozenset[str] | None] = {
    "collections": frozenset(
        {
            "ChainMap",
            "Counter",
            "OrderedDict",
            "defaultdict",
            "deque",
            "namedtuple",
        }
    ),
    "datetime": frozenset(
        {
            "MAXYEAR",
            "MINYEAR",
            "UTC",
            "date",
            "datetime",
            "time",
            "timedelta",
            "timezone",
            "tzinfo",
        }
    ),
    "decimal": frozenset(
        {
            "BasicContext",
            "Clamped",
            "Context",
            "ConversionSyntax",
            "Decimal",
            "DecimalException",
            "DefaultContext",
            "DivisionByZero",
            "DivisionImpossible",
            "DivisionUndefined",
            "ExtendedContext",
            "FloatOperation",
            "Inexact",
            "InvalidContext",
            "InvalidOperation",
            "Overflow",
            "ROUND_05UP",
            "ROUND_CEILING",
            "ROUND_DOWN",
            "ROUND_FLOOR",
            "ROUND_HALF_DOWN",
            "ROUND_HALF_EVEN",
            "ROUND_HALF_UP",
            "ROUND_UP",
            "Rounded",
            "Subnormal",
            "Underflow",
            "getcontext",
            "localcontext",
            "setcontext",
        }
    ),
    "fractions": frozenset({"Fraction"}),
    "functools": frozenset({"partial", "reduce"}),
    "itertools": None,
    "json": frozenset(
        {
            "JSONDecodeError",
            "JSONDecoder",
            "JSONEncoder",
            "dumps",
            "loads",
        }
    ),
    # math 的所有公开导出都是 C 数值函数或数值常量。
    "math": None,
    "re": frozenset(
        {
            "A",
            "ASCII",
            "DEBUG",
            "DOTALL",
            "I",
            "IGNORECASE",
            "L",
            "LOCALE",
            "M",
            "MULTILINE",
            "NOFLAG",
            "Pattern",
            "RegexFlag",
            "S",
            "U",
            "UNICODE",
            "VERBOSE",
            "X",
            "compile",
            "escape",
            "findall",
            "finditer",
            "fullmatch",
            "match",
            "search",
            "split",
            "sub",
            "subn",
        }
    ),
    "statistics": frozenset(
        {
            "StatisticsError",
            "NormalDist",
            "correlation",
            "covariance",
            "fmean",
            "geometric_mean",
            "harmonic_mean",
            "linear_regression",
            "mean",
            "median",
            "median_grouped",
            "median_high",
            "median_low",
            "mode",
            "multimode",
            "pstdev",
            "pvariance",
            "quantiles",
            "stdev",
            "variance",
        }
    ),
}
SAFE_MODULE_NAMES = frozenset(SAFE_MODULE_EXPORTS)

# 这些名字要么直接产生外部副作用，要么能绕开受限 builtins/AST 规则。
BANNED_CALL_NAMES = {
    "__import__",
    "breakpoint",
    "classmethod",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "object",
    "open",
    "print",
    "property",
    "setattr",
    "staticmethod",
    "super",
    "type",
    "vars",
}

SENSITIVE_PATTERNS = (
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "私钥正文"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    (re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"), "GitHub Token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "API Key"),
    (
        re.compile(
            r"(?i)(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[\"'][^\"']{8,}[\"']"
        ),
        "疑似内嵌凭据",
    ),
)

SENSITIVE_PATH_FRAGMENTS = {
    "/etc/passwd",
    "/etc/shadow",
    ".aws/credentials",
    ".config/gcloud",
    ".env",
    ".git-credentials",
    ".npmrc",
    ".ssh/",
    "appdata/roaming/microsoft/credentials",
    "id_ed25519",
    "id_rsa",
}


class SandboxPolicyError(ValueError):
    """脚本违反可公开返回的沙箱策略。"""


def _finding(code: str, message: str, *, line: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if line is not None:
        result["line"] = line
    return result


def _literal_is_safe(node: ast.AST) -> bool:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return False
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return False
    return len(encoded) <= 8_000


def _module_exports_name(module_name: str, export_name: str) -> bool:
    if module_name not in SAFE_MODULE_EXPORTS:
        return False
    exports = SAFE_MODULE_EXPORTS[module_name]
    return exports is None or export_name in exports


def analyze_source(source: str, *, script_name: str) -> dict[str, Any]:
    """返回稳定的静态检查报告；任何 finding 都会阻止执行。"""

    findings: list[dict[str, Any]] = []
    encoded = source.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) > MAX_SOURCE_BYTES:
        findings.append(
            _finding("source-too-large", f"脚本不得超过 {MAX_SOURCE_BYTES} UTF-8 bytes")
        )
    if "\x00" in source:
        findings.append(_finding("nul-byte", "脚本不得包含空字符"))
    for pattern, label in SENSITIVE_PATTERNS:
        if pattern.search(source):
            findings.append(_finding("embedded-secret", f"检测到{label}"))
    normalized_source = source.replace("\\", "/").lower()
    for fragment in sorted(SENSITIVE_PATH_FRAGMENTS):
        if fragment in normalized_source:
            findings.append(
                _finding("sensitive-path", f"检测到敏感路径片段: {fragment}")
            )

    tree: ast.Module | None = None
    try:
        tree = ast.parse(source, filename=script_name, mode="exec")
    except SyntaxError as exc:
        findings.append(
            _finding(
                "syntax-error",
                f"Python 语法错误: {exc.msg}",
                line=exc.lineno,
            )
        )

    imports: set[str] = set()
    module_aliases: dict[str, str] = {}
    node_count = 0
    if tree is not None:
        nodes = list(ast.walk(tree))
        node_count = len(nodes)
        if node_count > MAX_AST_NODES:
            findings.append(
                _finding("ast-too-complex", f"AST 节点不得超过 {MAX_AST_NODES}")
            )

        for statement in tree.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
                pass
            elif (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                pass
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if value is None or not _literal_is_safe(value):
                    findings.append(
                        _finding(
                            "top-level-side-effect",
                            "顶层赋值只能使用不超过 8KB 的 JSON 字面量",
                            line=statement.lineno,
                        )
                    )
            else:
                findings.append(
                    _finding(
                        "top-level-side-effect",
                        "顶层只允许导入、函数定义、文档字符串和常量",
                        line=getattr(statement, "lineno", None),
                    )
                )

        main_functions = [
            item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "main"
        ]
        if len(main_functions) != 1:
            findings.append(
                _finding("main-contract", "脚本必须且只能定义一个 main(data) 函数")
            )
        else:
            arguments = main_functions[0].args
            if (
                len(arguments.posonlyargs) + len(arguments.args) != 1
                or arguments.vararg is not None
                or arguments.kwarg is not None
                or arguments.kwonlyargs
                or arguments.defaults
            ):
                findings.append(
                    _finding(
                        "main-contract",
                        "main 必须只接收一个无默认值的位置参数 data",
                        line=main_functions[0].lineno,
                    )
                )

        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.YieldFrom)):
                findings.append(
                    _finding(
                        "unsupported-syntax",
                        f"受限脚本不允许 {type(node).__name__}",
                        line=getattr(node, "lineno", None),
                    )
                )
            if isinstance(node, (ast.FunctionDef, ast.Lambda)) and getattr(
                node, "decorator_list", []
            ):
                findings.append(
                    _finding(
                        "decorator",
                        "受限脚本不允许装饰器",
                        line=getattr(node, "lineno", None),
                    )
                )
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                findings.append(
                    _finding(
                        "private-attribute",
                        "受限脚本不允许访问下划线开头的属性",
                        line=node.lineno,
                    )
                )
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in module_aliases
            ):
                module_name = module_aliases[node.value.id]
                if (
                    module_name in SAFE_MODULE_EXPORTS
                    and not _module_exports_name(module_name, node.attr)
                ):
                    findings.append(
                        _finding(
                            "banned-module-attribute",
                            f"模块投影 {module_name} 不暴露属性 {node.attr}",
                            line=node.lineno,
                        )
                    )
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                findings.append(
                    _finding(
                        "dunder-name",
                        "受限脚本不允许双下划线名称",
                        line=node.lineno,
                    )
                )
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "__" in node.value
            ):
                findings.append(
                    _finding(
                        "dunder-string",
                        "受限脚本不允许构造双下划线属性名",
                        line=node.lineno,
                    )
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in BANNED_CALL_NAMES:
                    findings.append(
                        _finding(
                            "banned-call",
                            f"受限脚本不允许调用 {node.func.id}",
                            line=node.lineno,
                        )
                    )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    imports.add(root)
                    module_aliases[alias.asname or root] = root
                    if root not in SAFE_MODULE_NAMES or alias.name != root:
                        findings.append(
                            _finding(
                                "banned-import",
                                f"受限脚本不允许导入 {alias.name}",
                                line=node.lineno,
                            )
                        )
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".", 1)[0]
                imports.add(root)
                if (
                    node.level
                    or root not in SAFE_MODULE_NAMES
                    or module != root
                    or any(alias.name == "*" or alias.name.startswith("_") for alias in node.names)
                    or any(
                        not _module_exports_name(root, alias.name)
                        for alias in node.names
                    )
                ):
                    findings.append(
                        _finding(
                            "banned-import",
                            f"受限脚本不允许该导入: {module or '<relative>'}",
                            line=node.lineno,
                        )
                    )

    # 同一语义只报告一次，避免向调用方返回异常膨胀的结果。
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for item in findings:
        key = (item["code"], item["message"], item.get("line"))
        if key not in seen:
            seen.add(key)
            deduplicated.append(item)
        if len(deduplicated) >= 50:
            break
    return {
        "ok": not deduplicated,
        "policy_version": POLICY_VERSION,
        "script_name": script_name,
        "source_sha256": digest,
        "source_bytes": len(encoded),
        "ast_nodes": node_count,
        "imports": sorted(imports),
        "findings": deduplicated,
    }


def _install_os_limits() -> dict[str, str]:
    """在可用平台设置进程级资源上限，并如实返回实际生效情况。"""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        try:
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW 失败")
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.PerProcessUserTimeLimit = 20_000_000
            info.BasicLimitInformation.ActiveProcessLimit = 1
            info.BasicLimitInformation.LimitFlags = (
                0x00000002  # JOB_OBJECT_LIMIT_PROCESS_TIME
                | 0x00000008  # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | 0x00000100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            info.ProcessMemoryLimit = 256 * 1024 * 1024
            if not kernel32.SetInformationJobObject(
                job,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject 失败")
            if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject 失败")
            # 句柄必须存活到进程结束，否则 KILL_ON_JOB_CLOSE 会立即终止当前进程。
            globals()["_WINDOWS_JOB_HANDLE"] = job
            return {
                "cpu": "Windows Job Object: 2s process time",
                "memory": "Windows Job Object: 256MiB",
                "processes": "Windows Job Object: 1",
            }
        except (AttributeError, OSError, ctypes.ArgumentError) as exc:
            return {
                "cpu": "由父进程 wall timeout 限制",
                "memory": f"未获得 OS 强制上限: {type(exc).__name__}",
                "processes": "由语言策略禁止创建",
            }
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
        return {
            "cpu": "RLIMIT_CPU: 2s",
            "memory": "RLIMIT_AS: 256MiB",
            "processes": "由语言策略禁止创建",
        }
    except (ImportError, OSError, ValueError) as exc:
        return {
            "cpu": "由父进程 wall timeout 限制",
            "memory": f"未获得 OS 强制上限: {type(exc).__name__}",
            "processes": "由语言策略禁止创建",
        }


def _install_audit_policy() -> None:
    """拒绝生成代码可能触达的外部副作用审计事件。"""

    denied_exact = {
        "code.__new__",
        "function.__new__",
        "open",
        "os.chdir",
        "os.chmod",
        "os.chown",
        "os.exec",
        "os.fork",
        "os.kill",
        "os.link",
        "os.listdir",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.scandir",
        "os.spawn",
        "os.startfile",
        "os.symlink",
        "os.system",
        "subprocess.Popen",
        "sys._getframe",
        "sys.setprofile",
        "sys.settrace",
    }
    denied_prefixes = ("socket.", "winreg.", "ctypes.")

    def audit(event: str, _args: tuple[Any, ...]) -> None:
        if event in denied_exact or event.startswith(denied_prefixes):
            raise PermissionError(f"受限脚本触发被拒绝的审计事件: {event}")

    sys.addaudithook(audit)


def _safe_import_factory(modules: dict[str, Any]) -> Callable[..., Any]:
    def safe_import(
        name: str,
        _globals: dict[str, Any] | None = None,
        _locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level or name not in modules:
            raise ImportError(f"沙箱禁止导入: {name}")
        module = modules[name]
        if fromlist and any(item.startswith("_") for item in fromlist):
            raise ImportError("沙箱禁止导入私有名称")
        return module

    return safe_import


def _build_safe_modules() -> dict[str, Any]:
    """把标准库模块投影为逐项 allowlist，隐藏其内部 sys/operator 等引用。"""

    projected: dict[str, Any] = {}
    for name, configured_exports in SAFE_MODULE_EXPORTS.items():
        module = __import__(name)
        exports = (
            {item for item in vars(module) if not item.startswith("_")}
            if configured_exports is None
            else set(configured_exports)
        )
        missing = sorted(item for item in exports if not hasattr(module, item))
        if missing:
            raise RuntimeError(f"沙箱模块投影缺少导出 {name}: {missing}")
        projected[name] = types.SimpleNamespace(
            **{item: getattr(module, item) for item in sorted(exports)}
        )
    return projected


def _safe_builtins(safe_import: Callable[..., Any]) -> dict[str, Any]:
    return {
        "__import__": safe_import,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "Exception": Exception,
        "filter": filter,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "KeyError": KeyError,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "TypeError": TypeError,
        "ValueError": ValueError,
        "zip": zip,
    }


def execute_source(source: str, data: Any, *, script_name: str) -> dict[str, Any]:
    """执行已通过策略的 main(data)，并返回有界 JSON 结果。"""

    report = analyze_source(source, script_name=script_name)
    if not report["ok"]:
        return {"ok": False, "security": report, "error": "脚本未通过静态安全策略"}

    input_bytes = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(input_bytes) > MAX_INPUT_BYTES:
        raise SandboxPolicyError(f"脚本 JSON 输入不得超过 {MAX_INPUT_BYTES} bytes")

    # 先导入允许模块，再安装 audit hook，防止生成代码驱动新的磁盘导入。
    modules = _build_safe_modules()
    limits = _install_os_limits()
    _install_audit_policy()
    namespace: dict[str, Any] = {
        "__builtins__": _safe_builtins(_safe_import_factory(modules)),
        "__name__": "skill_script",
    }
    try:
        code = compile(source, script_name, "exec", dont_inherit=True, optimize=2)
        exec(code, namespace, namespace)
        result = namespace["main"](data)
        output = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (Exception, KeyboardInterrupt) as exc:
        return {
            "ok": False,
            "security": report,
            "limits": limits,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return {
            "ok": False,
            "security": report,
            "limits": limits,
            "error": f"脚本 JSON 输出超过 {MAX_OUTPUT_BYTES} bytes",
        }
    return {
        "ok": True,
        "security": report,
        "limits": limits,
        "result": json.loads(output),
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_SOURCE_BYTES + MAX_INPUT_BYTES + 20_000)
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise SandboxPolicyError("沙箱请求必须是 JSON 对象")
        source = request.get("source")
        script_name = request.get("script_name")
        action = request.get("action")
        if not isinstance(source, str) or not isinstance(script_name, str):
            raise SandboxPolicyError("沙箱请求缺少 source 或 script_name")
        if action == "analyze":
            result = {
                "ok": True,
                "security": analyze_source(source, script_name=script_name),
            }
        elif action == "execute":
            result = execute_source(source, request.get("input"), script_name=script_name)
        else:
            raise SandboxPolicyError("未知沙箱动作")
    except (
        json.JSONDecodeError,
        UnicodeError,
        SandboxPolicyError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
