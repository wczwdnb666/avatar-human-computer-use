# -*- coding: utf-8 -*-
"""从 UIAutomationClient.h 提取 UIA 接口的 **vtable 方法顺序**，用于校验索引。

为什么需要这个工具
------------------
纯 ctypes 调 COM 时，每个方法都靠"vtable 槽位下标"寻址，写错一位就会
静默调到别的方法上，症状往往是无理由的 ``E_INVALIDARG`` 或返回垃圾值。
网上的 UIA 封装（pywinauto / comtypes 的生成代码）索引各写各的，
唯一可信来源是 Windows SDK 头文件里的 C 风格 vtbl 结构：

    typedef struct IUIAutomationRangeValuePatternVtbl {
        BEGIN_INTERFACE
        HRESULT (STDMETHODCALLTYPE *QueryInterface)(...);   // 0
        ULONG   (STDMETHODCALLTYPE *AddRef)(...);           // 1
        ULONG   (STDMETHODCALLTYPE *Release)(...);          // 2
        HRESULT (STDMETHODCALLTYPE *SetValue)(...);         // 3  ← 业务方法从这里开始
        ...
    } IUIAutomationRangeValuePatternVtbl;

结构体里方法出现的**顺序**就是真实槽位顺序（含 IUnknown 的 0/1/2）。
本脚本把这份顺序打出来，和 ``avatar/uia/patterns.py`` 里的 ``VT_*`` 常量表
逐条对照即可确认没抄错。

用法
----
1. 下载头文件到仓库根目录（约 766 KB）：

       curl -L -o UIAutomationClient.h \\
         https://cdn.jsdelivr.net/gh/tpn/winsdk-10/Include/10.0.16299.0/um/UIAutomationClient.h

   （走 jsdelivr 镜像，raw.githubusercontent.com 在本机经常 RemoteDisconnected）

2. 全量打印：

       python tools/dump_uia_vtbl.py UIAutomationClient.h

3. 只看某个接口：

       python tools/dump_uia_vtbl.py UIAutomationClient.h RangeValue

4. 只看业务方法（跳过 IUnknown 的 0/1/2，下标从 3 开始）：

       python tools/dump_uia_vtbl.py UIAutomationClient.h Window --skip-unknown

依赖：仅标准库。
"""
import re
import sys

#: IUnknown 占用的前三个槽位，业务方法一律从 3 开始
UNKNOWN_SLOTS = ("QueryInterface", "AddRef", "Release")


def parse(path):
    """解析头文件，返回 ``{接口名: [方法名, ...]}``（顺序即槽位顺序）。"""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    out = {}
    # 每个接口形如:  typedef struct IXxxVtbl { ... } END_INTERFACE
    pattern = r"typedef struct (\w+)Vtbl\s*\{(.*?)END_INTERFACE"
    for match in re.finditer(pattern, text, re.S):
        iface = match.group(1)
        body = match.group(2)
        # 方法形如:  RET ( STDMETHODCALLTYPE *Name )(...);
        names = re.findall(
            r"\(\s*STDMETHODCALLTYPE\s*\*\s*(\w+)\s*\)\s*\(", body)
        if names:
            out[iface] = names
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2

    header = argv[1]
    keyword = argv[2] if len(argv) > 2 else None
    skip_unknown = "--skip-unknown" in argv

    vtables = parse(header)
    if not vtables:
        print("没解析到任何 vtbl 结构，确认头文件路径是否正确：%s" % header)
        return 1

    # 默认只关心模式接口；给了关键词就按关键词过滤
    if keyword:
        selected = {k: v for k, v in vtables.items() if keyword in k}
    else:
        selected = {k: v for k, v in vtables.items() if "Pattern" in k}

    if not selected:
        print("没有匹配的接口。可用的有：")
        for name in sorted(vtables):
            print("   ", name)
        return 1

    for iface in sorted(selected):
        methods = vtables[iface]
        print("=" * 66)
        print("%s   (%d 个方法)" % (iface, len(methods)))
        print("=" * 66)
        for index, name in enumerate(methods):
            if skip_unknown and name in UNKNOWN_SLOTS:
                continue
            tag = ""
            if name in UNKNOWN_SLOTS:
                tag = "   # IUnknown"
            print("  %2d  %s%s" % (index, name, tag))
        print("")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
