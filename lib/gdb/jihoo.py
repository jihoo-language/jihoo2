# gdb pretty printers for jihoo's standard library: `str` and `String` as
# text, `Vec(T)` and slices as their elements, `Map(K, V)` as its entries.
# Programs built with `-g` embed this file (`.debug_gdb_scripts`), which
# gdb loads where its auto-load safe path allows; or `source` it.

import gdb


def _bytes(ptr, length):
    if length <= 0:
        return ""
    data = gdb.selected_inferior().read_memory(ptr, min(length, 1 << 20))
    return bytes(data).decode("utf-8", "replace")


class StrPrinter:
    def __init__(self, val):
        self.val = val

    def to_string(self):
        return _bytes(self.val["data_ptr"], int(self.val["length"]))

    def display_hint(self):
        return "string"


class StringPrinter:
    def __init__(self, val):
        self.val = val

    def to_string(self):
        b = self.val["bytes"]
        return _bytes(b["ptr"], int(b["len"]))

    def display_hint(self):
        return "string"


class ElementsPrinter:
    """`Vec(T)` (`ptr`, `len`, `cap`) and slices (`data_ptr`, `length`)."""

    def __init__(self, val, ptr, length, label):
        self.val = val
        self.ptr = val[ptr]
        self.len = int(val[length])
        self.label = label

    def to_string(self):
        return "%s of length %d" % (self.label, self.len)

    def children(self):
        for i in range(min(self.len, 10000)):
            yield "[%d]" % i, (self.ptr + i).dereference()

    def display_hint(self):
        return "array"


class MapPrinter:
    def __init__(self, val):
        self.val = val

    def to_string(self):
        return "%s with %d entries" % (self.val.type.name, int(self.val["len"]))

    def children(self):
        state = self.val["state"]
        flags, cap = state["ptr"], int(self.val["cap"])
        keys, vals = self.val["keys"], self.val["vals"]
        n = 0
        for i in range(min(cap, 1 << 20)):
            if int((flags + i).dereference()) == 1:
                yield "[%d]" % n, (keys + i).dereference()
                yield "[%d]" % (n + 1), (vals + i).dereference()
                n += 2

    def display_hint(self):
        return "map"


def _fields(t):
    return {f.name for f in t.fields()} if t.code == gdb.TYPE_CODE_STRUCT else set()


def lookup(val):
    t = val.type.strip_typedefs()
    if t.code == gdb.TYPE_CODE_REF:
        return None
    name = t.name or ""
    fields = _fields(t)
    if name == "str" and fields == {"data_ptr", "length"}:
        return StrPrinter(val)
    if name == "String" and fields == {"bytes"}:
        return StringPrinter(val)
    if name.startswith("Vec(") and fields == {"ptr", "len", "cap"}:
        return ElementsPrinter(val, "ptr", "len", name)
    if name.startswith("[") and fields == {"data_ptr", "length"}:
        return ElementsPrinter(val, "data_ptr", "length", name)
    if name.startswith("Map(") and {"state", "keys", "vals", "cap", "len"} <= fields:
        return MapPrinter(val)
    return None


gdb.pretty_printers.append(lookup)
