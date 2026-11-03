"""Edit ECUC values while preserving all bytes outside changed XML subtrees.

The parsing, path lookup and template cloning approach follows the MIT licensed
DaVinci GW project; its notice is included in licenses/davinci-gw-MIT.txt.
This module does not claim to replace DaVinci Validate or Generate.
"""

from __future__ import annotations

import codecs
import os
import re
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import ClassVar
from uuid import uuid4
from xml.parsers import expat

from lxml import etree


def local_name(node: etree._Element) -> str:
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def child_text(node: etree._Element, name: str) -> str | None:
    child = next((item for item in node if local_name(item) == name), None)
    return child.text.strip() if child is not None and child.text is not None else None


def short_name(node: etree._Element) -> str | None:
    return child_text(node, "SHORT-NAME")


def definition_ref(node: etree._Element) -> str | None:
    return child_text(node, "DEFINITION-REF")


def autosar_path(node: etree._Element) -> str:
    names = []
    current = node
    while current is not None:
        name = short_name(current)
        if name:
            names.append(name)
        current = current.getparent()
    return "/" + "/".join(reversed(names))


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False,
        remove_blank_text=False, remove_comments=False, strip_cdata=False,
        huge_tree=True,
    )


def _parse(data: bytes) -> etree._ElementTree:
    # Reject declarations before parsing, including UTF-16 and UTF-32 sources.
    lexical = data.replace(b"\x00", b"")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", lexical, re.IGNORECASE):
        raise ValueError("ARXML must not contain a DTD or entity declaration.")
    tree = etree.fromstring(data, _parser()).getroottree()
    if tree.docinfo.doctype or any(isinstance(node, etree._Entity) for node in tree.getroot().iter()):
        raise ValueError("DTD and entity references are not supported.")
    if local_name(tree.getroot()) != "AUTOSAR" or not etree.QName(tree.getroot()).namespace:
        raise ValueError("Expected an AUTOSAR root with a namespace.")
    return tree


def _tag_end(data: bytes, start: int) -> int:
    quote = None
    for position in range(start, len(data)):
        char = data[position]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in (34, 39):
            quote = char
        elif char == 62:
            return position + 1
    raise ValueError("Unterminated XML tag.")


def _byte_spans(data: bytes, root: etree._Element) -> dict:
    """Match Expat byte offsets to lxml elements without inspecting XML text."""
    spans = []
    stack = []
    parser = expat.ParserCreate()

    def start(_name, _attrs):
        first = parser.CurrentByteIndex
        opening_end = _tag_end(data, first)
        record = [first, None, data[first:opening_end].rstrip().endswith(b"/>")]
        spans.append(record)
        stack.append(record)

    def end(_name):
        record = stack.pop()
        record[1] = parser.CurrentByteIndex if record[2] else _tag_end(data, parser.CurrentByteIndex)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.Parse(data, True)
    elements = [node for node in root.iter() if isinstance(node.tag, str)]
    if len(elements) != len(spans):
        raise ValueError("XML byte positions do not match parsed elements.")
    return {node: (record[0], record[1]) for node, record in zip(elements, spans)}


class XmlDocument:
    """Provide namespace aware ECUC editing and refuse to overwrite any output."""

    _ORDER: ClassVar[dict[str, int]] = {
        "SHORT-NAME": 10, "LONG-NAME": 20, "DESC": 30, "CATEGORY": 40,
        "ADMIN-DATA": 50, "INTRODUCTION": 60, "DEFINITION-REF": 70,
        "ANNOTATIONS": 80, "PARAMETER-VALUES": 90, "REFERENCE-VALUES": 100,
        "SUB-CONTAINERS": 110,
    }
    _KINDS: ClassVar[dict[str, tuple[str, str]]] = {
        "string": ("ECUC-TEXTUAL-PARAM-VALUE", "ECUC-STRING-PARAM-DEF"),
        "enum": ("ECUC-TEXTUAL-PARAM-VALUE", "ECUC-ENUMERATION-PARAM-DEF"),
        "integer": ("ECUC-NUMERICAL-PARAM-VALUE", "ECUC-INTEGER-PARAM-DEF"),
        "boolean": ("ECUC-NUMERICAL-PARAM-VALUE", "ECUC-BOOLEAN-PARAM-DEF"),
        "float": ("ECUC-NUMERICAL-PARAM-VALUE", "ECUC-FLOAT-PARAM-DEF"),
        "ref": ("ECUC-REFERENCE-VALUE", "ECUC-REFERENCE-DEF"),
        "symbolic-ref": ("ECUC-REFERENCE-VALUE", "ECUC-SYMBOLIC-NAME-REFERENCE-DEF"),
        "choice-ref": ("ECUC-REFERENCE-VALUE", "ECUC-CHOICE-REFERENCE-DEF"),
        "function": ("ECUC-TEXTUAL-PARAM-VALUE", "ECUC-FUNCTION-NAME-DEF"),
    }

    def __init__(self, path: str | Path):
        self.source_path = Path(path).expanduser().resolve()
        self.original = self.source_path.read_bytes()
        self.tree = _parse(self.original)
        self.root = self.tree.getroot()
        self.ns = etree.QName(self.root).namespace
        self.namespace = self.ns
        self.encoding = self.tree.docinfo.encoding or "UTF-8"
        self.newline = b"\r\n" if self.original.count(b"\r\n") > self.original.count(b"\n") // 2 else b"\n"
        self._snapshots = {
            node: (node.tag, dict(node.attrib), node.text, node.tail, tuple(node))
            for node in self.root.iter()
        }
        self._spans = None
        if codecs.lookup(self.encoding).name not in {"utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"}:
            self._spans = _byte_spans(self.original, self.root)
        self.containers = {}
        self.index()

    @classmethod
    def load(cls, path: str | Path):
        return cls(path)

    def q(self, name: str) -> str:
        return f"{{{self.ns}}}{name}"

    @staticmethod
    def path(node: etree._Element) -> str:
        return autosar_path(node)

    @staticmethod
    def name(node: etree._Element) -> str | None:
        return short_name(node)

    @staticmethod
    def kind(node: etree._Element) -> str:
        return (definition_ref(node) or "").rsplit("/", 1)[-1]

    def index(self) -> dict:
        self.containers = {}
        for node in self.root.iter():
            if local_name(node) not in {"ECUC-CONTAINER-VALUE", "ECUC-MODULE-CONFIGURATION-VALUES"}:
                continue
            path = self.path(node)
            if path in self.containers:
                raise ValueError(f"Duplicate AUTOSAR path: {path}")
            self.containers[path] = node
        return self.containers

    def at(self, path: str) -> etree._Element:
        return self.containers["/" + path.strip("/")]

    def module(self, name: str) -> etree._Element:
        found = [node for node in self.containers.values()
                 if local_name(node) == "ECUC-MODULE-CONFIGURATION-VALUES" and self.name(node) == name]
        if len(found) != 1:
            raise ValueError(f"Expected exactly one {name} module, found {len(found)}.")
        return found[0]

    def _entries(self, node: etree._Element, key: str) -> list:
        found = []
        for group in node:
            if local_name(group) not in {"PARAMETER-VALUES", "REFERENCE-VALUES"}:
                continue
            for entry in group:
                definition = definition_ref(entry) or ""
                if definition == key or (not key.startswith("/") and definition.rsplit("/", 1)[-1] == key):
                    found.append(entry)
        return found

    def values(self, node: etree._Element, key: str) -> list[str]:
        return [child.text or "" for entry in self._entries(node, key) for child in entry
                if local_name(child) in {"VALUE", "VALUE-REF"}]

    def value(self, node: etree._Element, key: str, default=None):
        values = self.values(node, key)
        if len(values) > 1:
            raise ValueError(f"Expected one value for {self.path(node)}/{key}, found {len(values)}.")
        return values[0] if values else default

    def _insert(self, parent, child):
        rank = self._ORDER.get(local_name(child), 1000)
        for index, existing in enumerate(parent):
            if self._ORDER.get(local_name(existing), 1000) > rank:
                parent.insert(index, child)
                return child
        parent.append(child)
        return child

    def _group(self, node, name):
        group = node.find(self.q(name))
        return group if group is not None else self._insert(node, etree.Element(self.q(name)))

    def set_value(self, node, key, value, kind="string", definition=None, dest=None):
        """Update one value, retaining existing entry type, attributes and DEST."""
        found = self._entries(node, key)
        if len(found) > 1:
            raise ValueError(f"Ambiguous existing value: {self.path(node)}/{key}")
        if found:
            entry = found[0]
            target = next((item for item in entry if local_name(item) in {"VALUE", "VALUE-REF"}), None)
            if target is None:
                raise ValueError(f"Value entry is missing VALUE or VALUE-REF: {key}")
        else:
            tag, parameter_dest = self._KINDS[kind]
            is_ref = kind.endswith("ref")
            group = self._group(node, "REFERENCE-VALUES" if is_ref else "PARAMETER-VALUES")
            entry = etree.SubElement(group, self.q(tag))
            definition_node = etree.SubElement(entry, self.q("DEFINITION-REF"), DEST=parameter_dest)
            definition_node.text = definition or (key if key.startswith("/") else f"{definition_ref(node)}/{key}")
            target = etree.SubElement(entry, self.q("VALUE-REF" if is_ref else "VALUE"))
            if is_ref:
                target.set("DEST", dest or "ECUC-CONTAINER-VALUE")
        target.text = str(value).lower() if isinstance(value, bool) else str(value)
        if dest and local_name(target) == "VALUE-REF":
            target.set("DEST", dest)
        return entry

    def append_value(self, node, key, value, kind="ref", definition=None, dest=None):
        """Append a repeated reference using its existing type where available."""
        entries = self._entries(node, key)
        if not entries:
            return self.set_value(node, key, value, kind, definition, dest)
        clone = deepcopy(entries[-1])
        target = next(child for child in clone if local_name(child) in {"VALUE", "VALUE-REF"})
        target.text = str(value)
        if dest and local_name(target) == "VALUE-REF":
            target.set("DEST", dest)
        entries[-1].getparent().append(clone)
        return clone

    def add_container(self, parent, name, definition, choice=False):
        """Create a named ECUC container in its schema ordered position."""
        group_name = "CONTAINERS" if local_name(parent) == "ECUC-MODULE-CONFIGURATION-VALUES" else "SUB-CONTAINERS"
        group = self._group(parent, group_name)
        if any(short_name(child) == name for child in group):
            raise ValueError(f"Container already exists: {self.path(parent)}/{name}")
        container = etree.Element(self.q("ECUC-CONTAINER-VALUE"), UUID=str(uuid4()))
        etree.SubElement(container, self.q("SHORT-NAME")).text = name
        destination = "ECUC-CHOICE-CONTAINER-DEF" if choice else "ECUC-PARAM-CONF-CONTAINER-DEF"
        etree.SubElement(container, self.q("DEFINITION-REF"), DEST=destination).text = definition
        group.append(container)
        self.containers[self.path(container)] = container
        return container

    def clone_container(self, source, parent, name):
        group_name = "CONTAINERS" if local_name(parent) == "ECUC-MODULE-CONFIGURATION-VALUES" else "SUB-CONTAINERS"
        group = self._group(parent, group_name)
        if any(short_name(child) == name for child in group):
            raise ValueError(f"Container already exists: {self.path(parent)}/{name}")
        clone = deepcopy(source)
        clone.find(self.q("SHORT-NAME")).text = name
        for node in clone.iter():
            if node.get("UUID") is not None:
                node.set("UUID", str(uuid4()))
        group.append(clone)
        for node in clone.iter():
            if local_name(node) == "ECUC-CONTAINER-VALUE":
                self.containers[self.path(node)] = node
        return clone

    def _fragment(self, node) -> bytes:
        data = etree.tostring(node, encoding=self.encoding, with_tail=False, pretty_print=False)
        parent = node.getparent()
        if parent is not None:
            end = _tag_end(data, 0)
            opening = data[:end]
            for prefix, uri in parent.nsmap.items():
                attribute = b"xmlns" + (b":" + prefix.encode("ascii") if prefix else b"")
                escaped = uri.replace("&", "&amp;").replace('"', "&quot;").encode(self.encoding)
                opening = opening.replace(b" " + attribute + b'="' + escaped + b'"', b"")
            data = opening + data[end:]
        return data.replace(b"\n", self.newline)

    def _patches(self, node):
        old = self._snapshots.get(node)
        if old is None:
            raise ValueError("Cannot replace a node without an original byte position.")
        current = (node.tag, dict(node.attrib), node.text, node.tail, tuple(node))
        children_same = current[4] == old[4]
        tail_changes = any(self._snapshots.get(child, (None,) * 4)[3] != child.tail for child in node)
        if current[:3] != old[:3] or not children_same or tail_changes:
            start, end = self._spans[node]
            return [(start, end, self._fragment(node))]
        patches = []
        for child in node:
            if not isinstance(child.tag, str):
                if child.text != self._snapshots[child][2] or dict(child.attrib) != self._snapshots[child][1]:
                    start, end = self._spans[node]
                    return [(start, end, self._fragment(node))]
            else:
                patches.extend(self._patches(child))
        return patches

    def to_bytes(self) -> bytes:
        if self._spans is not None:
            result = self.original
            for start, end, replacement in reversed(self._patches(self.root)):
                result = result[:start] + replacement + result[end:]
        else:
            declaration = self.original.replace(b"\x00", b"").lstrip(b"\xef\xbb\xbf\xff\xfe").startswith(b"<?xml")
            options = {"encoding": self.encoding, "xml_declaration": declaration, "pretty_print": False}
            if self.tree.docinfo.standalone in (True, False) and re.search(br"standalone\s*=", self.original.replace(b"\x00", b"")):
                options["standalone"] = self.tree.docinfo.standalone
            result = etree.tostring(self.tree, **options)
            if self.newline == b"\r\n":
                result = result.decode(self.encoding).replace("\n", "\r\n").encode(self.encoding)
        _parse(result)
        return result

    def save_new(self, path: str | Path, validator: Callable | None = None) -> Path:
        """Validate a temporary file then publish it atomically without replacing."""
        target = Path(path).expanduser().resolve()
        if target == self.source_path or target.exists():
            raise FileExistsError(f"Output must be a new file: {target}")
        if not target.parent.is_dir():
            raise FileNotFoundError(f"Output directory does not exist: {target.parent}")
        data = self.to_bytes()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if validator:
                validator(XmlDocument(temporary))
            # A hard link publishes the complete file and fails if a competing
            # writer has created the target since the initial existence check.
            os.link(temporary, target)
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


ArxmlDocument = XmlDocument
