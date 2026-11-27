#!/usr/bin/env python3
"""Drive `jihoo lsp` as an editor would, over the Language Server Protocol."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, compiler, encodings):
        self.process = subprocess.Popen(
            [str(compiler), "lsp"], cwd=ROOT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0
        self.notifications = []
        capabilities = {"general": {"positionEncodings": encodings}} if encodings else {}
        result = self.request("initialize", {"processId": None, "rootUri": None,
                                             "capabilities": capabilities})
        self.capabilities = result["capabilities"]
        assert result["serverInfo"]["name"] == "jihoo", result
        self.notify("initialized", {})

    def send(self, message):
        body = json.dumps(message).encode()
        self.process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.process.stdin.flush()

    def receive(self):
        length = None
        while True:
            line = self.process.stdout.readline()
            assert line, self.process.stderr.read()
            line = line.strip()
            if not line:
                break
            name, value = line.split(b":", 1)
            if name.lower() == b"content-length":
                length = int(value)
        return json.loads(self.process.stdout.read(length))

    def request(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == self.next_id and "method" not in message:
                assert "error" not in message, message
                return message["result"]
            self.notifications.append(message)

    def request_error(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == self.next_id:
                return message["error"]
            self.notifications.append(message)

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def diagnostics(self, uri):
        """The next diagnostics published for `uri`."""
        while True:
            for i, message in enumerate(self.notifications):
                if (message.get("method") == "textDocument/publishDiagnostics"
                        and message["params"]["uri"] == uri):
                    del self.notifications[i]
                    return message["params"]["diagnostics"]
            self.notifications.append(self.receive())

    def close(self):
        assert self.request("shutdown", None) is None
        self.notify("exit", None)
        assert self.process.wait(timeout=30) == 0
        assert not self.process.stderr.read()


def uri(path):
    return path.resolve().as_uri()


def position(text, offset, utf8):
    before = text.encode()[:offset].decode()
    line = before.count("\n")
    column = before[before.rfind("\n") + 1:]
    units = len(column.encode()) if utf8 else len(column.encode("utf-16-le")) // 2
    return {"line": line, "character": units}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=ROOT / "out/jihoo")
    compiler = parser.parse_args().compiler.resolve()

    with tempfile.TemporaryDirectory(prefix="jihoo-lsp-") as directory:
        directory = Path(directory)
        helper = directory / "helper.jh"
        helper.write_text("pub fn twice(x: i64) -> i64 { x * 2 }\n")
        main_file = directory / "main.jh"
        main_file.write_text("fn main() {}\n")
        source = ("import std.mem\nimport helper\n\n"
                  "struct Point {\n    x: i64\n}\n\n"
                  "struct Bag {\n    items: Vec(String)\n}\n\n"
                  "fn main() {\n"
                  "    let s = \"가😀\"; let value = helper.twice(2)\n"
                  "    var v = Vec(String).new()\n"
                  "    let p = Point { x: value }\n"
                  "    let old = mem.replace(&v, Vec(String).new())\n"
                  "    let bag = Bag { items: Vec(String).new() }\n"
                  "    println(value + p.x + old.len() + s.len + bag.items.len() + missing)\n"
                  "}\n")
        error_offset = source.encode().index(b"missing")

        for encodings, utf8 in [(["utf-8", "utf-16"], True), (None, False)]:
            client = Client(compiler, encodings)
            assert client.capabilities["positionEncoding"] == ("utf-8" if utf8 else "utf-16")
            assert client.capabilities["definitionProvider"] is True

            # Unsaved text is checked, not the file on disk.
            client.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri(main_file), "languageId": "jihoo", "version": 1, "text": source}})
            diagnostics = client.diagnostics(uri(main_file))
            assert len(diagnostics) == 1, diagnostics
            diagnostic = diagnostics[0]
            assert "missing" in diagnostic["message"] and diagnostic["severity"] == 1, diagnostic
            assert diagnostic["range"]["start"] == position(source, error_offset, utf8), diagnostic
            assert diagnostic["range"]["end"] == position(source, error_offset + 7, utf8), diagnostic

            def definition(word, occurrence=0, after=0):
                offset = source.encode().index(word.encode(), after)
                for _ in range(occurrence):
                    offset = source.encode().index(word.encode(), offset + 1)
                return client.request("textDocument/definition", {
                    "textDocument": {"uri": uri(main_file)},
                    "position": position(source, offset + 1, utf8)})

            # A local, after a multibyte one on the same line.
            found = definition("value", 2)
            assert found["uri"] == uri(main_file), found
            assert found["range"]["start"] == position(source, source.encode().index(b"value"), utf8)
            # An item of an imported module, and the module itself.
            found = definition("twice")
            assert found["uri"] == uri(helper) and found["range"]["start"] == {"line": 0, "character": 7}
            found = definition("helper", 1)
            assert found["uri"] == uri(helper) and found["range"]["start"] == {"line": 0, "character": 0}
            # A struct, its field, and items of the standard library.
            found = definition("Point", 1)
            assert found["range"]["start"] == position(source, source.encode().index(b"Point"), utf8)
            found = definition("x", 0, source.encode().index(b"p.x"))
            assert found["range"]["start"] == position(source, source.encode().index(b"x: i64"), utf8)
            found = definition("replace")
            assert found["uri"] == uri(ROOT / "lib/std/mem.jh"), found
            found = definition("Vec")
            assert found["uri"] == uri(ROOT / "lib/std/vec.jh"), found

            # Fixing the error clears it.
            fixed = source.replace(" + missing", "")
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 2},
                                                     "contentChanges": [{"text": fixed}]})
            assert client.diagnostics(uri(main_file)) == []

            # Hover: a local's type as the compiler inferred it, and an
            # item's or field's declaration.
            assert client.capabilities["hoverProvider"] is True

            def hover(word, occurrence=0, after=0):
                offset = fixed.encode().index(word.encode(), after)
                for _ in range(occurrence):
                    offset = fixed.encode().index(word.encode(), offset + 1)
                found = client.request("textDocument/hover", {
                    "textDocument": {"uri": uri(main_file)},
                    "position": position(fixed, offset + 1, utf8)})
                return found["contents"]["value"] if found else None

            assert "value: i64" in hover("value", 2), hover("value", 2)
            assert "v: Vec(String)" in hover("v", 0, fixed.encode().index(b"var v") + 4), hover("v", 0, fixed.encode().index(b"var v") + 4)
            assert "old: Vec(String)" in hover("old", 1), hover("old", 1)
            assert "s: str" in hover("s", 0, fixed.encode().index(b"let s")), hover("s")
            assert "p: Point" in hover("p", 0, fixed.encode().index(b"p.x")), hover("p")
            assert "pub fn twice(x: i64) -> i64" in hover("twice"), hover("twice")
            assert "struct Point" in hover("Point", 1), hover("Point", 1)
            assert "x: i64" in hover("x", 0, fixed.encode().index(b"p.x")), hover("x")
            # `value.name` by the receiver's type: Vec's `len`, not every `len`.
            offset = fixed.encode().index(b"old.len") + 4
            found = client.request("textDocument/definition", {
                "textDocument": {"uri": uri(main_file)},
                "position": position(fixed, offset + 1, utf8)})
            assert isinstance(found, dict) and found["uri"] == uri(ROOT / "lib/std/vec.jh"), found
            assert "pub fn len(self) -> i64" in hover("len", 0, fixed.encode().index(b"old.len")), \
                hover("len", 0, fixed.encode().index(b"old.len"))
            # Through a field: `bag.items.len()` is Vec's too.
            offset = fixed.encode().index(b"bag.items.len") + 10
            found = client.request("textDocument/definition", {
                "textDocument": {"uri": uri(main_file)},
                "position": position(fixed, offset + 1, utf8)})
            assert isinstance(found, dict) and found["uri"] == uri(ROOT / "lib/std/vec.jh"), found
            assert "items: Vec(String)" in hover("items", 1), hover("items", 1)
            # A trait's default method: Show's `to_string`, not String's.
            fixed2 = fixed.replace("    let bag =", "    let shown = p.x.to_string()\n    let bag =")
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 100},
                                                     "contentChanges": [{"text": fixed2}]})
            assert client.diagnostics(uri(main_file)) == []
            offset = fixed2.encode().index(b"p.x.to_string") + 4
            found = client.request("textDocument/definition", {
                "textDocument": {"uri": uri(main_file)},
                "position": position(fixed2, offset + 1, utf8)})
            assert isinstance(found, dict) and found["uri"] == uri(ROOT / "lib/std/prelude.jh"), found
            # Receivers that are calls and indexes: `origin().x` is Point's
            # field, `bag.items[0].len()` String's method.
            fixed3 = fixed.replace("fn main() {", "fn origin() -> Point { Point { x: 1 } }\n\nfn main() {").replace(
                "    println(", "    let ox = origin().x + bag.items[0].len()\n    println(")
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 102},
                                                     "contentChanges": [{"text": fixed3}]})
            assert client.diagnostics(uri(main_file)) == []
            offset = fixed3.encode().index(b"origin().x") + 9
            found = client.request("textDocument/definition", {
                "textDocument": {"uri": uri(main_file)},
                "position": position(fixed3, offset, utf8)})
            assert isinstance(found, dict) and found["uri"] == uri(main_file), found
            assert found["range"]["start"] == position(fixed3, fixed3.encode().index(b"x: i64"), utf8), found
            offset = fixed3.encode().index(b"[0].len") + 5
            found = client.request("textDocument/definition", {
                "textDocument": {"uri": uri(main_file)},
                "position": position(fixed3, offset, utf8)})
            assert isinstance(found, dict) and found["uri"] == uri(ROOT / "lib/std/string.jh"), found
            # The outline: items, with fields, variants and methods inside.
            assert client.capabilities["documentSymbolProvider"] is True
            outline = ("enum Shape {\n    Dot\n    Square(i64)\n}\n\n"
                       "impl Show for Shape {\n    fn to_string(self) -> String { String.from(\"s\") }\n}\n\n"
                       "const LIMIT: i64 = 3\n")
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 103},
                                                     "contentChanges": [{"text": fixed + "\n" + outline}]})
            symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri(main_file)}})
            names = [(s["name"], s["kind"]) for s in symbols]
            assert names == [("Point", 23), ("Bag", 23), ("main", 12), ("Shape", 10),
                             ("impl Show for Shape", 19), ("LIMIT", 14)], names
            point = symbols[0]
            assert point["selectionRange"]["start"] == position(fixed, fixed.encode().index(b"Point"), utf8), point
            assert [(c["name"], c["kind"]) for c in point["children"]] == [("x", 8)], point
            shape = symbols[3]
            assert [c["name"] for c in shape["children"]] == ["Dot", "Square"], shape
            assert [(c["name"], c["kind"]) for c in symbols[4]["children"]] == [("to_string", 12)], symbols[4]
            # References.
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 105},
                                                     "contentChanges": [{"text": fixed}]})
            assert client.diagnostics(uri(main_file)) == []
            assert client.capabilities["referencesProvider"] is True

            def references(word, occurrence, after, declaration):
                offset = fixed.encode().index(word.encode(), after)
                for _ in range(occurrence):
                    offset = fixed.encode().index(word.encode(), offset + 1)
                found = client.request("textDocument/references", {
                    "textDocument": {"uri": uri(main_file)},
                    "position": position(fixed, offset + 1, utf8),
                    "context": {"includeDeclaration": declaration}})
                return sorted((r["uri"], r["range"]["start"]["line"], r["range"]["start"]["character"])
                              for r in found)
            def at(offset, file=None):
                start = position(fixed, offset, utf8)
                return (file or uri(main_file), start["line"], start["character"])
            src = fixed.encode()
            # A local's uses, then with its declaration.
            declared = src.index(b"value")
            uses = sorted([at(src.index(b"x: value") + 3), at(src.index(b"(value") + 1)])
            assert references("value", 0, 0, False) == uses, references("value", 0, 0, False)
            assert references("value", 2, 0, True) == sorted(uses + [at(declared)])
            # A field: its declaration, the struct literal's field and `p.x`.
            found = references("x", 0, src.index(b"p.x"), True)
            assert found == sorted([at(src.index(b"x: i64")), at(src.index(b"x: value")),
                                    at(src.index(b"p.x") + 2)]), found
            # Vec's `len`, not `str`'s (`s.len`): `old.len()` and
            # `bag.items.len()`, besides those in other files.
            found = [r for r in references("len", 0, src.index(b"old.len"), False)
                     if r[0] == uri(main_file)]
            assert found == sorted([at(src.index(b"old.len") + 4),
                                    at(src.index(b"items.len") + 6)]), found
            # An item of another module, from its use.
            found = references("twice", 0, 0, True)
            assert found == sorted([(uri(helper), 0, 7), at(src.index(b"twice"))]), found
            # Rename: every edit, in each file that changes.
            assert client.capabilities["renameProvider"] is True

            def rename_params(word, after, new_name):
                return {"textDocument": {"uri": uri(main_file)},
                        "position": position(fixed, src.index(word.encode(), after) + 1, utf8),
                        "newName": new_name}

            def edits(result):
                return sorted((u, e["range"]["start"]["line"], e["range"]["start"]["character"],
                               e["newText"]) for u, es in result["changes"].items() for e in es)
            renamed = client.request("textDocument/rename", rename_params("value", 0, "amount"))
            assert edits(renamed) == sorted(x + ("amount",) for x in uses + [at(declared)]), renamed
            renamed = client.request("textDocument/rename", rename_params("x", src.index(b"p.x"), "y"))
            assert edits(renamed) == sorted(x + ("y",) for x in [
                at(src.index(b"x: i64")), at(src.index(b"x: value")), at(src.index(b"p.x") + 2)]), renamed
            # An item of another module, in that module's file too.
            renamed = client.request("textDocument/rename", rename_params("twice", 0, "double"))
            assert edits(renamed) == sorted([(uri(helper), 0, 7, "double"),
                                             at(src.index(b"twice")) + ("double",)]), renamed
            # A use the search does not see (in another module that is not
            # open) makes the renamed program fail to check: refused.
            helper.write_text("pub fn twice(x: i64) -> i64 { x * 2 }\n"
                              "pub fn quad(x: i64) -> i64 { twice(twice(x)) }\n")
            error = client.request_error("textDocument/rename", rename_params("twice", 0, "double"))
            assert "would not check" in error["message"], error
            helper.write_text("pub fn twice(x: i64) -> i64 { x * 2 }\n")
            # Refused: not a name, a name already in the document, the
            # standard library's `len`.
            error = client.request_error("textDocument/rename", rename_params("value", 0, "2x"))
            assert "is not a name" in error["message"], error
            error = client.request_error("textDocument/rename", rename_params("value", 0, "old"))
            assert "`old` is already used" in error["message"], error
            error = client.request_error("textDocument/rename", rename_params("len", src.index(b"old.len"), "size"))
            assert "standard library" in error["message"], error
            # Unparsable text has no outline.
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 104},
                                                     "contentChanges": [{"text": "fn main( {\n"}]})
            assert client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri(main_file)}}) is None
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 101},
                                                     "contentChanges": [{"text": fixed}]})
            assert client.diagnostics(uri(main_file)) == []
            # The comment above a declaration comes with it.
            assert "Puts v in the place" in hover("replace"), hover("replace")
            assert client.request("textDocument/hover", {
                "textDocument": {"uri": uri(main_file)},
                "position": {"line": 2, "character": 0}}) is None

            # A burst of edits that arrive together is checked once, with the last text.
            burst = b""
            for version in range(3, 8):
                text = fixed.replace("fn main() {", "fn main() {\n    let e%d = nope%d" % (version, version))
                body = json.dumps({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {
                    "textDocument": {"uri": uri(main_file), "version": version},
                    "contentChanges": [{"text": text}]}}).encode()
                burst += b"Content-Length: %d\r\n\r\n" % len(body) + body
            client.process.stdin.write(burst)
            client.process.stdin.flush()
            diagnostics = client.diagnostics(uri(main_file))
            assert [d["message"] for d in diagnostics] == ["unknown name `nope7`"], diagnostics

            # Formatting is one edit of the whole document.
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 9},
                                                     "contentChanges": [{"text": "fn main() {\n  let x=1\n}\n"}]})
            edits = client.request("textDocument/formatting", {"textDocument": {"uri": uri(main_file)},
                                                               "options": {"tabSize": 4, "insertSpaces": True}})
            assert edits == [{"range": {"start": {"line": 0, "character": 0},
                                        "end": {"line": 3, "character": 0}},
                              "newText": "fn main() {\n    let x = 1\n}\n"}], edits

            assert client.diagnostics(uri(main_file)) == []

            # An error inside a generic instance is shown at the use in the document.
            generic = (ROOT / "tests/fail/threads_instance_note.jh").read_text()
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(main_file), "version": 10},
                                                     "contentChanges": [{"text": generic}]})
            diagnostics = client.diagnostics(uri(main_file))
            assert len(diagnostics) == 1, diagnostics
            diagnostic = diagnostics[0]
            assert diagnostic["range"]["start"] == {"line": 8, "character": 4}, diagnostic
            assert "each_mut" in diagnostic["message"], diagnostic
            related = diagnostic["relatedInformation"]
            assert related[0]["location"]["uri"] == uri(ROOT / "lib/std/thread.jh"), related
            assert "in the instance" in related[1]["message"], related

            error = client.request_error("textDocument/completion", {
                "textDocument": {"uri": uri(main_file)}, "position": {"line": 0, "character": 0}})
            assert error["code"] == -32601, error

            # A long check runs aside: a request sent while the compiler's
            # own main.jh is checked is answered before its diagnostics.
            big = ROOT / "compiler/main.jh"
            client.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri(big), "languageId": "jihoo", "version": 1, "text": big.read_text()}})
            time.sleep(0.2)
            client.next_id += 1
            client.send({"jsonrpc": "2.0", "id": client.next_id, "method": "textDocument/formatting",
                         "params": {"textDocument": {"uri": uri(main_file)},
                                    "options": {"tabSize": 4, "insertSpaces": True}}})
            order = []
            while len(order) < 2:
                message = client.receive()
                if message.get("id") == client.next_id:
                    order.append("reply")
                elif (message.get("method") == "textDocument/publishDiagnostics"
                      and message["params"]["uri"] == uri(big)):
                    order.append("diagnostics")
                    assert message["params"]["diagnostics"] == [], message
                else:
                    client.notifications.append(message)
            assert order == ["reply", "diagnostics"], order
            # An edit during a check cancels it: the first diagnostics after
            # it are for the new text, not the stale result.
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(big), "version": 2},
                                                     "contentChanges": [{"text": big.read_text()}]})
            time.sleep(0.2)
            broken = big.read_text() + "\nfn broken() { nope }\n"
            client.notify("textDocument/didChange", {"textDocument": {"uri": uri(big), "version": 3},
                                                     "contentChanges": [{"text": broken}]})
            diagnostics = client.diagnostics(uri(big))
            assert [d["message"] for d in diagnostics] == ["unknown name `nope`"], diagnostics
            client.notify("textDocument/didClose", {"textDocument": {"uri": uri(big)}})
            assert client.diagnostics(uri(big)) == []

            # Closing clears what the document published.
            client.notify("textDocument/didClose", {"textDocument": {"uri": uri(main_file)}})
            assert client.diagnostics(uri(main_file)) == []
            client.close()

        # Requests before initialization fail; exit without shutdown is status 1.
        process = subprocess.Popen([str(compiler), "lsp"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "shutdown"}).encode()
        exit_body = json.dumps({"jsonrpc": "2.0", "method": "exit"}).encode()
        output, errors = process.communicate(
            b"Content-Length: %d\r\n\r\n%s" % (len(body), body) +
            b"Content-Length: %d\r\n\r\n%s" % (len(exit_body), exit_body), timeout=30)
        assert process.returncode == 1 and not errors, errors
        assert b'"code":-32002' in output, output

        result = subprocess.run([str(compiler), "lsp", "main.jh"], capture_output=True)
        assert result.returncode == 2 and b"lsp takes no files" in result.stderr

    print("Language server: diagnostics, definitions, hover, references, rename, outline, formatting and lifecycle passed")


if __name__ == "__main__":
    main()
