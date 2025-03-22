# Copyright 2004-2025 Tom Rothamel <pytom@bishoujo.us>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

# This file contains the AST for the Ren'Py script language. Each class
# here corresponds to a statement in the script language.

# NOTE:
# When updating this file, consider if lint.py or warp.py also need
# updating.

# pyright: reportUnusedParameter=false, reportUnusedCallResult=false, reportFunctionMemberAccess=false
# pyright: reportUninitializedInstanceVariable=false, reportIncompatibleVariableOverride=false
# pyright: reportIncompatibleMethodOverride=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Any, Callable, ClassVar, Literal, Never, cast, override

import time
import hashlib
import ast
import re
import sys
import zlib

import renpy

from renpy.cslots import Object, Slot, IntegerSlot
from renpy.astsupport import hash32, PyExpr

from renpy.parameter import (
    ParameterInfo,
    ArgumentInfo,
    apply_arguments,
    EMPTY_PARAMETERS,
)

# For pickle compatibility.
if True:
    from renpy.parameter import (
        Parameter,
        Signature,
        EMPTY_ARGUMENTS,
    )


# Config variables that are set twice - once when the rpy is first loaded,
# and then again at init time.
EARLY_CONFIG = {
    "save_directory",
    "allow_duplicate_labels",
    "keyword_after_python",
    "steam_appid",
    "name",
    "version",
    "save_token_keys",
    "check_conflicting_properties",
    "check_translate_none",
    "defer_tl_scripts",
    "munge_in_strings",
    "interface_layer",
}


class PyCode(Object):

    _cslot_linenumbers: bool = True

    filename: str
    source: str
    location: tuple[int, ...]
    mode: Literal["eval", "exec", "hide"] = "eval"
    bytecode: bytes | None
    py: int = 3
    hashcode: int

    @override
    def __getstate__(self):
        return (
            1,
            self.source,
            (self.filename, self.linenumber),
            self.mode,
            self.py,
            self.hashcode,
            self.col_offset,
        )

    def __setstate__(self, state: Any):
        col_offset = 0
        py = 2
        hashcode = None

        match state:
            case (_, source, location, mode, py, hashcode, col_offset):
                pass
            case (_, source, location, mode, py, hashcode):
                pass
            case (_, source, location, mode, py):
                pass
            case (_, source, location, mode):
                pass
            case _:
                raise Exception("Invalid state:", state)

        self.py = py
        self.source = source
        self.filename = location[0]
        self.linenumber: int = location[1]
        self.col_offset: int = col_offset
        self.mode = mode

        if hashcode is None:
            if isinstance(source, PyExpr):
                self.hashcode = source.hashcode
            else:
                self.hashcode = hash32(source)

        self.bytecode = None

        if renpy.game.script.record_pycode:
            renpy.game.script.all_pycode.append(self)

    def __init__(
        self,
        source: str,
        loc: tuple[str, int] = ("<none>", 1),
        mode: Literal["eval", "exec", "hide"] = "exec",
    ):

        self.py = 3

        if isinstance(source, PyExpr):
            self.filename = source.filename
            self.linenumber = source.linenumber
            self.hashcode = source.hashcode
        else:
            self.filename = loc[0]
            self.linenumber = loc[1]
            self.hashcode = hash32(source)

        # The source code.
        if self.mode != "eval":
            self.source, self.col_offset = PyCode.dedent(source)
        else:
            self.source = source
            self.col_offset = 0

        self.mode = mode

        # This will be initialized later on, after we are serialized.
        self.bytecode = None

        if renpy.game.script.record_pycode:
            renpy.game.script.all_pycode.append(self)

    _leading_whitespace_re: re.Pattern[str] = re.compile(
        "(^[ ]*)(?:[^ ])", re.MULTILINE
    )

    @staticmethod
    def dedent(text: str):
        """
        Removes leading whitespace from a block of text. Entirely blank lines
        are normalized to a newline character.

        This returns the dedented text, and the amount of whitespace removed,
        """

        # Look for the longest leading string of spaces and tabs common to
        # all lines.
        margin = None
        indents = PyCode._leading_whitespace_re.findall(text)
        for indent in indents:
            if margin is None:
                margin = indent

            # Current line more deeply indented than previous winner:
            # no change (previous winner is still on top).
            elif indent.startswith(margin):
                pass

            # Current line consistent with and no deeper than previous winner:
            # it's the new winner.
            elif margin.startswith(indent):
                margin = indent

            # Find the largest common whitespace between current line and previous
            # winner.
            else:
                for i, (x, y) in enumerate(zip(margin, indent)):
                    if x != y:
                        margin = margin[:i]
                        break

        if margin:
            text = re.sub(r"(?m)^" + margin, "", text)

        if margin:
            return text, len(margin)
        else:
            return text, 0


DoesNotExtend: renpy.object.Sentinel = renpy.object.Sentinel("DoesNotExtend")


class Scry(object):
    """
    This is used to store information about the future, if we know it. Unlike
    predict, this tries to only get things we _know_ will happen.
    """

    _next: "Node | None" = None
    interacts: bool = False

    say: bool = False
    menu_with_caption: bool = False
    who: str | None = None

    extend_text: str | None | renpy.object.Sentinel = None
    """
    Text that will be added to the current say statement by a call to
    extend.
    """

    multiple: int | None = None
    "When the next say statement has a multiple argument, this is the value of that argument."

    # By default, all attributes are None.
    def __getattr__(self, name: str) -> Any:
        return None

    @override
    def __reduce__(self):
        raise Exception("Cannot pickle Scry.")

    def next(self) -> "Scry | None":
        if self._next is None:
            return None
        else:
            try:
                return self._next.scry()
            except Exception:
                return None


type NodeName = "str | tuple[Any, ...] | None"
type RollbackType = Literal["normal", "never", "force"]
# Workaround that IntegerSlot accept only unsigned int.
# By using type alias SignedInt slot will fail 'type is int' check.
type SignedInt = int


class Node(Object):
    """
    A node in the abstract syntax tree of the program.
    """

    _cslot_linenumbers: bool = True

    filename: str
    "Elided string file name of this node."

    _name: NodeName
    """
    Unique name of the node of all nodes in the abstract syntax tree.

    This is used when the node name is either a string, or doesn't fit
    into the usual filename, version, serial format.
    """

    name_version: int
    """
    When the name is a three-argument tuple, stores the version number.
    """

    name_serial: int
    """
    When the name is a three-argument tuple, stores the serial number.
    """

    next: "Node | None"
    """
    Node that unconditionally follows this one in the abstract syntax tree,
    or None if this node is the last one in the block.
    """

    translatable: ClassVar[bool] = False
    """
    True if this node is translatable, False otherwise.
    (This can be set on the class or the instance.)
    """

    translation_relevant: ClassVar[bool] = False
    """
    True if the node is relevant to translation, and has to be processed by
    take_translations.
    """

    rollback: ClassVar[RollbackType] = "normal"
    """
    How does the node participate in rollback?

    * "normal" in normal mode.
    * "never" generally never.
    * "force" force it to start.
    """

    warp: ClassVar[bool] = False
    """
    True if this statement should be run while warping, False otherwise.
    """

    @property
    def name(self) -> NodeName:
        """
        The name property stores and retreives the name for the node.
        This is one of:

        * A string, when the node is a label.
        * A tuple, in (filename, version, serial) format. This is stored efficently,
          as it makes up most nodes in Ren'Py.
        * Longer tuples, like (filename, version, serial, ...) are rare, but used.
        * None, when the name is not known.
        """

        if self._name:
            return self._name
        elif self.name_version:
            return (self.filename, self.name_version, self.name_serial)
        else:
            return None

    @name.setter
    def name(self, value: NodeName):

        match value:
            case (self.filename, int(version), int(serial)):
                self._name = None
                self.name_version = version
                self.name_serial = serial
            case _:
                self._name = value

    # An ast node is equal to its name, allowing it to be used as the key in renpy.script.Script.namemap.

    @override
    def __hash__(self):
        return hash(self.name)

    @override
    def __eq__(self, other: Any):
        return self.name == other

    # Statement_start used to be a property on all nodes.
    @property
    def statement_start(self) -> "Node":
        return self

    @statement_start.setter
    def statement_start(self, value: Never):
        pass

    def __init__(self, loc: tuple[str, int]):
        """
        Initializes this Node object.

        `loc`
            A (filename, physical line number) tuple giving the
            logical line on which this Node node starts.
        """

        self.filename = loc[0]
        self.linenumber: int = loc[1]

        self.name = None
        self.next = None

    def diff_info(self) -> tuple[Any, ...]:
        """
        Returns a tuple of diff info about ourself. This is used to
        compare Nodes to see if they should be considered the same node. The
        tuple returned must be hashable.
        """

        return (id(self),)

    def get_children(self, f: Callable[["Node"], Any]) -> None:
        """
        Calls `f` with this node and its children.
        """

        f(self)

    def execute_init(self):
        """
        Called at init time (that is, before the normal start of the script.),
        at init priority returned by `Node.get_init` to execute init code of
        this statement.
        """

    def get_init(self) -> int | None:
        """
        Return an integer priority for this node, or None if this node doesn't
        care to suggest one.
        """

        return None

    def chain(self, next: "Node | None") -> None:
        """
        This is called with the Node node that should be followed after
        executing this node, and all nodes that this node
        executes. (For example, if this node is a block label, the
        next is the node that should be executed after all nodes in
        the block.)
        """

        self.next = next

    def replace_next(self, old: "Node", new: "Node") -> None:
        """
        Replaces instances of the `old` node with `new` when it is the next
        node.
        """

        if self.next is old:
            self.next = new

    def execute(self) -> None:
        """
        Causes this node to execute, and any action it entails to be
        performed. The node should call next_node with the node to
        be executed after this one.
        """

        raise Exception("Node subclass forgot to define execute.")

    def early_execute(self) -> None:
        """
        Called when the module is loaded.
        """

    def predict(self) -> list["Node | None"]:
        """
        This is called to predictively load images from this node. It
        should cause renpy.display.predict.displayable and
        renpy.display.predict.screen to be called as necessary.

        Returns a list of nodes that may follow this one, where more
        likely to be executed first is earlier in the list.
        """

        if self.next is not None:
            return [self.next]
        else:
            return []

    def scry(self) -> Scry:
        """
        Called to return an object with some general, user-definable
        information about the future.
        """

        rv = Scry()
        rv._next = self.next  # pyright: ignore[reportPrivateUsage]
        return rv

    def restructure(self, callback: Callable[[list["Node"]], Any]):
        """
        Called to restructure the AST.

        When this method is called, callback is called once for each child
        block of the node. The block, a list, can be updated by the callback
        using slice assignment to the list.
        """

        # Does nothing for nodes that do not contain child blocks.
        return

    def get_code(self, dialogue_filter: Callable[[str], str] | None = None) -> str:
        """
        Returns the canonical form of the code corresponding to this statement.
        This only needs to be defined if the statement is translatable.

        `dialogue_filter`
            If present, a filter that should be applied to human-readable
            text in the statement.
        """

        raise Exception("Not Implemented")

    def analyze(self) -> None:
        """
        Called on all code after the init phase, to analyze it.
        """

        # Does nothing by default.
        return

    def can_warp(self) -> bool:
        """
        Returns true if this should be run while warping, False otherwise.
        """

        return self.warp

    def get_reachable(self) -> list["Node"]:
        """
        Return a possibly empty list of nodes that are directly reachable via
        this node.

        Basically, this should return all nodes that can be set as next node in
        `execute`, but unlike predict, it should not guess nodes, and return
        information that is statically defined in the node.
        """

        if self.next is None:
            return []
        else:
            return [self.next]

    def get_translation_strings(self) -> list[tuple[int, str]]:
        """
        Return a possibly empty list of linenumber, string pairs of strings
        that are additional translation strings for this node.
        """

        return []


################################################################################
# Utility functions
################################################################################


# The name of the current statement.
current_statement_name: str = "init"


def statement_name(name: str):
    """
    Reports the name of this statement to systems like window auto.
    """

    global current_statement_name
    current_statement_name = name

    for i in renpy.config.statement_callbacks:
        i(name)


def next_node(n: Node | None):
    """
    Indicates the next node that should be executed. When a statement
    can crash, this should be set as early as possible, so that ignore
    can bring us there.
    """

    renpy.game.context().next_node = n


def probably_side_effect_free(expr: str) -> bool:
    """
    Returns true if an expr probably does not have side effects, and should
    be predicted. Basically, this just whitelists a set of characters that
    doesn't allow for a function call.
    """

    return not ("(" in expr)


def chain_block(block: list[Node], next: Node | None) -> None:
    """
    This is called to chain together all of the nodes in a block. Node
    n is chained with node n+1, while the last node is chained with
    next.
    """

    if not block:
        return

    for a, b in zip(block, block[1:]):
        a.chain(b)

    block[-1].chain(next)


def say_menu_with(expression: str | None, callback: Callable[[Any], Any]):
    """
    This handles the with clause of a say or menu statement.
    """

    if expression is not None:
        what = renpy.python.py_eval(expression)
    elif renpy.store.default_transition and renpy.game.preferences.transitions == 2:
        what = renpy.store.default_transition
    else:
        return

    if not what:
        return

    if renpy.game.preferences.transitions:
        callback(what)


def eval_who(who: str | None, fast: bool | None = None) -> Any | None:
    """
    Evaluates the `who` parameter to a say statement.
    """

    if who is None:
        return None

    if "store.character" in renpy.python.store_dicts:
        rv = renpy.python.store_dicts["store.character"].get(who, None)
    else:
        rv = None

    if rv is None:
        rv = renpy.python.store_dicts["store"].get(who, None)

    if rv is not None:
        return rv

    try:
        return renpy.python.py_eval(who)
    except Exception:
        raise Exception("Sayer '%s' is not defined." % who)


type ImspecType = """
tuple[tuple[str, ...], str | None, str | None, list[str], str | None, str | None, list[str]] |
tuple[tuple[str, ...], str | None, str | None, list[str], str | None, str | None] |
tuple[tuple[str, ...], list[str], str | None]
"""


def predict_imspec(
    imspec: ImspecType, scene: bool = False, atl: "renpy.atl.RawBlock | None" = None
):
    """
    Call this to use the given callback to predict the image named
    in imspec.
    """

    if len(imspec) == 7:
        name, expression, tag, at_expr_list, layer, _zorder, _behind = imspec

    elif len(imspec) == 6:
        name, expression, tag, at_expr_list, layer, _zorder = imspec

    else:
        name, at_expr_list, layer = imspec
        tag = None
        expression = None

    if expression:
        try:
            img = renpy.python.py_eval(expression)
            img = renpy.easy.displayable(img)
        except Exception:
            return
    else:
        img = None

    at_list = []
    for i in at_expr_list:
        try:
            at_list.append(renpy.python.py_eval(i))
        except Exception:
            pass

    if atl is not None:
        try:
            at_list.append(renpy.display.transform.ATLTransform(atl))
        except Exception:
            pass

    layer = renpy.exports.default_layer(layer, tag or name, bool(expression))

    if scene:
        renpy.game.context().images.predict_scene(layer)

    renpy.exports.predict_show(name, layer, what=img, tag=tag, at_list=at_list)


def show_imspec(imspec: ImspecType, atl: "renpy.atl.RawBlock | None" = None):

    if len(imspec) == 7:
        name, expression, tag, at_list, layer, zorder, behind = imspec

    elif len(imspec) == 6:
        name, expression, tag, at_list, layer, zorder = imspec
        behind = []

    else:
        name, at_list, layer = imspec
        expression = None
        tag = None
        zorder = None
        behind = []

    if zorder is not None:
        zorder = renpy.python.py_eval(zorder)
    else:
        zorder = None

    if expression is not None:
        expression = renpy.python.py_eval(expression)

        if not renpy.config.old_show_expression:

            if isinstance(expression, str):
                name = expression

            else:

                if tag is None:

                    counter = 0

                    while True:
                        tag = "_show_expression_%d" % counter
                        if not renpy.exports.showing(tag, layer):
                            break

                        counter += 1

                name = tag

        expression = renpy.easy.displayable(expression)

    at_list = [renpy.python.py_eval(i) for i in at_list]

    layer = renpy.exports.default_layer(
        layer, tag or name, bool(expression) and (tag is None)
    )

    renpy.config.show(
        name,
        at_list=at_list,
        layer=layer,
        what=expression,
        zorder=zorder,
        tag=tag,
        behind=behind,
        atl=atl,
    )


def create_store(name: str):
    if name in renpy.config.special_namespaces:
        return

    # Take first two components of dot-joined name
    maybe_special = ".".join(name.split(".")[:2])
    if maybe_special in renpy.config.special_namespaces:
        if not renpy.config.special_namespaces[maybe_special].allow_child_namespaces:
            raise Exception(
                "Creating stores within the {} namespace is not supported.".format(
                    maybe_special[6:]
                )
            )

    renpy.python.create_store(name)


class StoreNamespace:
    pure: bool = True
    repeat_at_default_time: bool = False

    def __init__(self, store: str):
        self.store: str = store

    def set(self, name: str, value: Any):
        renpy.python.store_dicts[self.store][name] = value

    def set_default(self, name: str, value: Any):
        renpy.python.store_dicts[self.store][name] = value

    def get(self, name: str) -> Any:
        return renpy.python.store_dicts[self.store][name]


def get_namespace(store: str) -> tuple[StoreNamespace, bool]:
    """
    Returns the namespace object for `store`, and a flag that is true if the
    namespace is special, and false if it is a normal store.
    """

    if store in renpy.config.special_namespaces:
        return renpy.config.special_namespaces[store], True

    return StoreNamespace(store), False


def redefine(stores: list[str]):
    """
    Re-runs the define statements in the given stores.
    """

    for i in define_statements:
        i.redefine(stores)


def _reach_any(source: Node, target: Node) -> bool:
    return True


def get_reachable_nodes(
    entry_nodes: list[Node],
    node_validator: Callable[[Node, Node], bool] = _reach_any,
    seen: set[Node] | None = None,
) -> list[Node]:
    """
    Starting with `entry_nodes`, tries to reach new nodes by calling
    `node_validator` on each new node that is reachable from the node in set.

    `node_validator`
        A function that takes two nodes as arguments, the node from which
        reachability is being checked, and the node that is reachable from
        that node.
        If it returns True if the node should be considered
        reachable for that source node, otherwise it is skipped.

        By default allow any pairs of nodes.

    `seen`
        If not None, a set of nodes that have already been seen.
        This can be used to avoid redundant work for multiple calls to this
        function.

    Ends when no new nodes are found and returns the left of all reached nodes.
    Order of result nodes is all entry nodes, than nodes reachable from them,
    and so on.

    If all creator defined statements define their `reachable` function properly,
    this function should return the same result for all calls with the same
    arguments.
    """

    from collections import deque

    result: list[Node] = []
    worklist = deque(entry_nodes)

    if seen is None:
        seen = set()

    while worklist:
        node = worklist.popleft()
        if node in seen:
            continue

        seen.add(node)
        result.append(node)

        for n in node.get_reachable():
            if n in seen:
                continue

            if not node_validator(node, n):
                continue

            worklist.append(n)

    return result


################################################################################
# Basic statements
################################################################################


class Say(Node):

    who: str | None
    who_fast: bool
    what: str
    with_: str | None
    interact: bool = True
    attributes: tuple[str, ...] | None = None
    arguments: ArgumentInfo | None = None
    temporary_attributes: tuple[str, ...] | None = None
    rollback: RollbackType = "normal"  # type: ignore
    identifier: str | None = None
    explicit_identifier: bool = False

    @override
    def diff_info(self):
        return (Say, self.who, self.what)

    def __init__(
        self,
        loc: tuple[str, int],
        who: str | None,
        what: str,
        with_: str | None,
        interact: bool = True,
        attributes: tuple[str, ...] | None = None,
        arguments: ArgumentInfo | None = None,
        temporary_attributes: tuple[str, ...] | None = None,
        identifier: str | None = None,
    ):

        super(Say, self).__init__(loc)

        if who is not None:
            # True if who is a simple enough expression we can just look it up.
            if re.match(renpy.lexer.word_regexp + r"\s*$", who):
                self.who_fast = True
                self.who = sys.intern(who.strip())
            else:
                self.who_fast = False
                self.who = who

        else:
            self.who = None
            self.who_fast = False

        self.what = what
        self.with_ = with_
        self.interact = interact
        self.arguments = arguments

        # A tuple of attributes that are applied to the character that's
        # speaking, or None to disable this behavior.
        self.attributes = attributes

        # Ditto for temporary attributes.
        self.temporary_attributes = temporary_attributes

        # If given, write in the identifier.
        if identifier is not None:
            self.identifier = identifier
            self.explicit_identifier = True

    @override
    def get_code(self, dialogue_filter: Callable[[str], str] | None = None):
        rv: list[str] = []

        if self.who:
            rv.append(self.who)

        if self.attributes is not None:
            rv.extend(self.attributes)

        if self.temporary_attributes:
            rv.append("@")
            rv.extend(self.temporary_attributes)

        what = self.what
        if dialogue_filter is not None:
            what = dialogue_filter(what)

        rv.append(renpy.translation.encode_say_string(what))

        if not self.interact:
            rv.append("nointeract")

        if getattr(self, "identifier", None) and self.explicit_identifier:
            rv.append("id")
            rv.append(getattr(self, "identifier", None))

        if self.arguments:
            rv.append(self.arguments.get_code())

        # This has to be at the end.
        if self.with_:
            rv.append("with")
            rv.append(self.with_)

        return " ".join(rv)

    @override
    def execute(self):

        next_node(self.next)

        try:

            renpy.game.context().say_attributes = self.attributes
            renpy.game.context().temporary_attributes = self.temporary_attributes

            who = eval_who(self.who, self.who_fast)

            stmt_name: str = "say"
            if who is not None:
                stmt_name = getattr(who, "statement_name", "say")

                if callable(stmt_name):
                    stmt_name = stmt_name()

            statement_name(stmt_name)

            if not ((who is None) or callable(who) or isinstance(who, str)):
                raise Exception(f"Sayer {self.who!r} is not a function or string.")

            what = self.what
            if renpy.config.say_menu_text_filter:
                what = renpy.config.say_menu_text_filter(what)

            renpy.store._last_raw_what = what

            if self.arguments is not None:
                args, kwargs = self.arguments.evaluate()
            else:
                args = ()
                kwargs = {}

            kwargs.setdefault("interact", self.interact)

            if getattr(who, "record_say", True):
                renpy.store._last_say_who = self.who
                renpy.store._last_say_what = what
                renpy.store._last_say_args = args
                renpy.store._last_say_kwargs = kwargs

            say_menu_with(self.with_, renpy.game.interface.set_transition)
            renpy.exports.say(who, what, *args, **kwargs)

        finally:
            renpy.game.context().say_attributes = None
            renpy.game.context().temporary_attributes = None
            renpy.store._last_raw_what = ""

    @override
    def predict(self):

        old_attributes = renpy.game.context().say_attributes
        old_temporary_attributes = renpy.game.context().temporary_attributes

        try:

            renpy.game.context().say_attributes = self.attributes
            renpy.game.context().temporary_attributes = self.temporary_attributes

            who = eval_who(self.who, self.who_fast)

            def predict_with(trans):
                renpy.display.predict.displayable(
                    trans(old_widget=None, new_widget=None)
                )

            try:
                say_menu_with(self.with_, predict_with)
            except Exception:
                pass

            what = self.what
            if renpy.config.say_menu_text_filter:
                what = renpy.config.say_menu_text_filter(what)

            renpy.exports.predict_say(who, what)

        finally:
            renpy.game.context().say_attributes = old_attributes
            renpy.game.context().temporary_attributes = old_temporary_attributes

        return [self.next]

    def scry(self):
        rv = super().scry()

        who = eval_who(self.who, self.who_fast)
        rv.who = who
        rv.say = True

        try:
            rv.multiple = self.arguments.evaluate()[1]["multiple"]
        except Exception:
            pass

        if self.interact:
            renpy.exports.scry_say(who, self.what, rv)
        else:
            rv.interacts = False
            rv.extend_text = DoesNotExtend

        return rv


# Copy the descriptor.
setattr(Say, "with", Say.with_)


class Init(Node):

    block: list[Node]
    priority: SignedInt

    def __init__(self, loc: tuple[str, int], block: list[Node], priority: SignedInt):
        super(Init, self).__init__(loc)

        self.block = block
        self.priority = priority

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for i in self.block:
            i.get_children(f)

    @override
    def execute_init(self):
        renpy.execution.not_infinite_loop(60)
        renpy.game.context().run(self.block[0])

    @override
    def get_init(self):
        return self.priority

    # We handle chaining specially. We want to chain together the nodes in
    # the block, but we want that chain to end in None, and we also want
    # this node to just continue on to the next node in normal execution.
    @override
    def chain(self, next: Node | None):
        self.next: Node | None = next

        chain_block(self.block, None)

    @override
    def execute(self):
        next_node(self.next)
        renpy.execution.not_infinite_loop(60)
        statement_name("init")

    @override
    def restructure(self, callback):
        callback(self.block)


class Label(Node):

    translation_relevant: bool = True

    block: list[Node]
    parameters: ParameterInfo | None = None
    hide: bool = False

    def __init__(
        self,
        loc: tuple[str, int],
        name: str,
        block: list[Node],
        parameters: ParameterInfo | None = None,
        hide: bool = False,
    ):
        """
        Constructs a new Label node.

        @param name: The name of this label.
        @param block: A (potentially empty) list of nodes making up the
        block associated with this label.
        """

        super(Label, self).__init__(loc)

        self.name: str = name  # type: ignore
        self.block = block
        self.parameters = parameters
        self.hide = hide

    @override
    def diff_info(self):
        return (Label, self.name)

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for i in self.block:
            i.get_children(f)

    @override
    def chain(self, next):

        if self.block:
            self.next = self.block[0]
            chain_block(self.block, next)
        else:
            self.next = next

    @override
    def execute(self):
        next_node(self.next)
        statement_name("label")

        renpy.game.context().mark_seen()

        values = apply_arguments(
            self.parameters, renpy.store._args, renpy.store._kwargs
        )

        renpy.exports.dynamic(**values)

        renpy.store._args = None
        renpy.store._kwargs = None

        renpy.easy.run_callbacks(
            renpy.config.label_callback, self.name, renpy.game.context().last_abnormal
        )
        renpy.easy.run_callbacks(
            renpy.config.label_callbacks, self.name, renpy.game.context().last_abnormal
        )

    @override
    def restructure(self, callback):
        callback(self.block)


class Python(Node):

    code: PyCode
    store: str = "store"
    hide: bool = False

    def __init__(
        self,
        loc: tuple[str, int],
        python_code: str,
        hide: bool = False,
        store: str = "store",
    ):
        """
        @param code: A PyCode object.

        @param hide: If True, the code will be executed with its
        own local dictionary.
        """

        super(Python, self).__init__(loc)

        self.hide = hide

        if hide:
            self.code = PyCode(python_code, loc=loc, mode="hide")
        else:
            self.code = PyCode(python_code, loc=loc, mode="exec")

        self.store = store

    @override
    def diff_info(self):
        return (Python, self.code.source)

    @override
    def early_execute(self):
        renpy.python.create_store(self.store)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("python")

        try:
            renpy.python.py_exec_bytecode(
                self.code.bytecode, self.hide, store=self.store
            )
        finally:
            if not renpy.game.context().init_phase:
                for i in renpy.config.python_callbacks:
                    i()

    @override
    def scry(self):
        rv = super().scry()
        rv.interacts = True
        return rv


class EarlyPython(Node):

    code: PyCode
    store: str = "store"
    hide: bool = False

    def __init__(
        self,
        loc: tuple[str, int],
        python_code: str,
        hide: bool = False,
        store: str = "store",
    ):
        """
        @param code: A PyCode object.

        @param hide: If True, the code will be executed with its
        own local dictionary.
        """

        super(EarlyPython, self).__init__(loc)

        self.hide = hide

        if hide:
            self.code = PyCode(python_code, loc=loc, mode="hide")
        else:
            self.code = PyCode(python_code, loc=loc, mode="exec")

        self.store = store

    @override
    def diff_info(self):
        return (EarlyPython, self.code.source)

    @override
    def execute(self):
        next_node(self.next)
        renpy.execution.not_infinite_loop(60)
        statement_name("python early")

    @override
    def early_execute(self):
        renpy.python.create_store(self.store)

        if self.code.bytecode:
            renpy.python.py_exec_bytecode(
                self.code.bytecode, self.hide, store=self.store
            )


class Image(Node):

    imgname: tuple[str, ...]
    code: PyCode | None
    atl: renpy.atl.RawBlock | None

    def __init__(
        self,
        loc: tuple[str, int],
        name: tuple[str, ...],
        expr: str | None = None,
        atl: renpy.atl.RawBlock | None = None,
    ):
        """
        @param name: The name of the image being defined.

        @param expr: An expression yielding a Displayable that is
        assigned to the image.
        """

        super(Image, self).__init__(loc)

        self.imgname = name

        if expr:
            self.code = PyCode(expr, loc=loc, mode="eval")
            self.atl = None
        else:
            self.code = None
            self.atl = atl

    @override
    def diff_info(self):
        return (Image, tuple(self.imgname))

    @override
    def execute(self):

        # Note: We should always check that self.code is None before
        # accessing self.atl, as self.atl may not always exist.

        next_node(self.next)
        statement_name("image")

        if self.code is not None:
            img = renpy.python.py_eval_bytecode(self.code.bytecode)
        else:
            img = renpy.display.motion.ATLTransform(self.atl)

        renpy.exports.image(self.imgname, img)

    @override
    def analyze(self):
        if getattr(self, "atl", None) is not None:
            # ATL images must participate with the game defined
            # constant names. So, we pass empty parameters to enable it.
            self.atl.analyze(EMPTY_PARAMETERS)


class Transform(Node):

    varname: str
    atl: renpy.atl.RawBlock
    parameters: ParameterInfo | None = None
    store: str = "store"

    default_parameters: Signature = EMPTY_PARAMETERS

    def __init__(
        self,
        loc: tuple[str, int],
        store: str,
        name: str,
        atl: renpy.atl.RawBlock,
        parameters: ParameterInfo | None = default_parameters,
    ):

        super(Transform, self).__init__(loc)

        self.store = store
        self.varname = name
        self.atl = atl
        self.parameters = parameters

    @override
    def diff_info(self):
        return (Transform, self.store, self.varname)

    @override
    def early_execute(self):
        create_store(self.store)

    @override
    def execute(self):

        next_node(self.next)
        statement_name("transform")

        parameters = getattr(self, "parameters", None)

        if parameters is None:
            parameters = Transform.default_parameters

        trans = renpy.display.motion.ATLTransform(self.atl, parameters=parameters)
        renpy.dump.transforms.append((self.varname, self.filename, self.linenumber))
        renpy.exports.pure(self.varname)

        ns, _special = get_namespace(self.store)
        ns.set(self.varname, trans)

    @override
    def analyze(self):

        parameters = getattr(self, "parameters", None)

        if parameters is None:
            parameters = Transform.default_parameters

        self.atl.analyze(parameters)


class Show(Node):

    imspec: ImspecType
    atl: renpy.atl.RawBlock | None = None

    warp: bool = True

    def __init__(
        self,
        loc: tuple[str, int],
        imspec: ImspecType,
        atl: renpy.atl.RawBlock | None = None,
    ):
        """
        @param imspec: A triple consisting of an image name (itself a
        tuple of strings), a list of at expressions, and a layer.
        """

        super(Show, self).__init__(loc)

        self.imspec = imspec
        self.atl = atl

    @override
    def diff_info(self):
        return (Show, tuple(self.imspec[0]))

    @override
    def execute(self):
        next_node(self.next)
        statement_name("show")

        show_imspec(self.imspec, atl=getattr(self, "atl", None))

    @override
    def predict(self):
        predict_imspec(self.imspec, atl=getattr(self, "atl", None))
        return [self.next]

    @override
    def analyze(self):
        if getattr(self, "atl", None) is not None:
            # ATL block defined for show, scene or show layer statements
            # must participate with the game defined constant names.
            # So, we pass empty parameters to enable it.
            self.atl.analyze(EMPTY_PARAMETERS)


class ShowLayer(Node):

    warp: bool = True

    at_list: list[str]
    atl: renpy.atl.RawBlock | None = None
    layer: str = "master"

    def __init__(
        self,
        loc: tuple[str, int],
        layer: str = "master",
        at_list: list[str] | None = None,
        atl: renpy.atl.RawBlock | None = None,
    ):
        super(ShowLayer, self).__init__(loc)

        if at_list is None:
            at_list = []

        self.layer = layer
        self.at_list = at_list
        self.atl = atl

    @override
    def diff_info(self):
        return (ShowLayer, self.layer)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("show layer")

        at_list = [renpy.python.py_eval(i) for i in self.at_list]

        if self.atl is not None:
            atl = renpy.display.motion.ATLTransform(self.atl)
            at_list.append(atl)

        renpy.exports.layer_at_list(at_list, layer=self.layer)

    @override
    def predict(self):
        return [self.next]

    @override
    def analyze(self):
        if self.atl is not None:
            self.atl.analyze(EMPTY_PARAMETERS)


class Camera(Node):

    warp: bool = True

    at_list: list[str]
    atl: renpy.atl.RawBlock | None
    layer: str = "master"

    def __init__(
        self,
        loc: tuple[str, int],
        layer: str = "master",
        at_list: list[str] | None = None,
        atl: renpy.atl.RawBlock | None = None,
    ):
        super(Camera, self).__init__(loc)

        if at_list is None:
            at_list = []

        self.layer = layer
        self.at_list = at_list
        self.atl = atl

    @override
    def diff_info(self):
        return (Camera, self.layer)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("show layer")

        at_list = [renpy.python.py_eval(i) for i in self.at_list]

        if self.atl is not None:
            atl = renpy.display.motion.ATLTransform(self.atl)
            at_list.append(atl)

        renpy.exports.layer_at_list(at_list, layer=self.layer, camera=True)

    @override
    def predict(self):
        return [self.next]

    @override
    def analyze(self):
        if self.atl is not None:
            self.atl.analyze(EMPTY_PARAMETERS)


class Scene(Node):

    imspec: ImspecType
    atl: renpy.atl.RawBlock | None = None
    layer: str = "master"

    warp: bool = True

    def __init__(
        self,
        loc: tuple[str, int],
        imgspec: ImspecType,
        layer: str = "master",
        atl: renpy.atl.RawBlock | None = None,
    ):
        """
        @param imspec: A triple consisting of an image name (itself a
        tuple of strings), a list of at expressions, and a layer, or
        None to not have this scene statement also display an image.
        """

        super(Scene, self).__init__(loc)

        self.imspec = imgspec
        self.layer = layer
        self.atl = atl

    @override
    def diff_info(self):
        if self.imspec:
            data = tuple(self.imspec[0])
        else:
            data = None

        return (Scene, data)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("scene")

        renpy.config.scene(self.layer)

        if self.imspec:
            show_imspec(self.imspec, atl=getattr(self, "atl", None))

    @override
    def predict(self):
        if self.imspec:
            predict_imspec(self.imspec, atl=getattr(self, "atl", None), scene=True)

        return [self.next]

    @override
    def analyze(self):
        if getattr(self, "atl", None) is not None:
            self.atl.analyze(EMPTY_PARAMETERS)


class Hide(Node):

    warp: bool = True

    imspec: ImspecType

    def __init__(self, loc: tuple[str, int], imgspec: ImspecType):
        """
        @param imspec: A triple consisting of an image name (itself a
        tuple of strings), a list of at expressions, and a list of
        with expressions.
        """

        super(Hide, self).__init__(loc)

        self.imspec = imgspec

    @override
    def diff_info(self):
        return (Hide, tuple(self.imspec[0]))

    @override
    def predict(self):

        if len(self.imspec) == 7:
            name, _expression, tag, _at_list, layer, _zorder, _behind = self.imspec

        elif len(self.imspec) == 6:
            name, _expression, tag, _at_list, layer, _zorder = self.imspec
            _behind = None
        else:
            name, _at_list, layer = self.imspec
            tag = None
            _expression = None
            _zorder = None
            _behind = None

        if tag is None:
            tag = name[0]

        layer = renpy.exports.default_layer(layer, tag)

        renpy.game.context().images.predict_hide(layer, (tag,))

        return [self.next]

    @override
    def execute(self):

        next_node(self.next)
        statement_name("hide")

        if len(self.imspec) == 7:
            name, _expression, tag, _at_list, layer, _zorder, _behind = self.imspec
        elif len(self.imspec) == 6:
            name, _expression, tag, _at_list, layer, _zorder = self.imspec
        else:
            name, _at_list, layer = self.imspec
            _expression = None
            tag = None
            _zorder = 0

        layer = renpy.exports.default_layer(layer, tag or name)

        renpy.config.hide(tag or name, layer)


class With(Node):

    expr: str
    paired: str | None = None

    def __init__(self, loc: tuple[str, int], expr: str, paired: str | None = None):
        """
        @param expr: An expression giving a transition or None.
        """

        super(With, self).__init__(loc)
        self.expr = expr
        self.paired = paired

    @override
    def diff_info(self):
        return (With, self.expr)

    @override
    def execute(self):

        next_node(self.next)
        statement_name("with")

        trans = renpy.python.py_eval(self.expr)

        if self.paired is not None:
            paired = renpy.python.py_eval(self.paired)
        else:
            paired = None

        renpy.exports.with_statement(trans, paired=paired)

    @override
    def predict(self):

        try:
            trans = renpy.python.py_eval(self.expr)

            if trans:
                renpy.display.predict.displayable(
                    trans(old_widget=None, new_widget=None)
                )

        except Exception:
            pass

        return [self.next]


class Call(Node):

    label: str
    arguments: ArgumentInfo | None = None
    expression: bool = False
    global_label: str = ""

    def __init__(
        self,
        loc: tuple[str, int],
        label: str,
        expression: bool = False,
        arguments: ArgumentInfo | None = None,
        global_label: str = "",
    ):

        super(Call, self).__init__(loc)
        self.label = label
        self.expression = expression
        self.arguments = arguments
        self.global_label = global_label

    @override
    def diff_info(self):
        return (Call, self.label, self.expression)

    @override
    def execute(self):

        statement_name("call")

        label = self.label
        if self.expression:
            label = renpy.python.py_eval(label)

            if isinstance(label, str) and label.startswith("."):
                label = self.global_label + label

        rv = renpy.game.context().call(label, return_site=self.next.name)
        next_node(rv)
        renpy.game.context().abnormal = True

        if self.arguments:
            args, kwargs = self.arguments.evaluate()
            renpy.store._args = args
            renpy.store._kwargs = kwargs
        else:
            renpy.store._args = None
            renpy.store._kwargs = None

    @override
    def predict(self) -> list[Node]:

        label = self.label

        if self.expression:

            if not probably_side_effect_free(label):
                return []

            try:
                label = renpy.python.py_eval(label)
            except Exception:
                return []

            if isinstance(label, str) and label.startswith("."):
                label = self.global_label + label

            if not renpy.game.script.has_label(label):
                return []

        return [renpy.game.context().predict_call(label, self.next.name)]

    @override
    def scry(self):
        rv = super().scry()
        rv._next = None  # pyright: ignore[reportPrivateUsage]
        return rv

    @override
    def get_reachable(self):
        rv = super().get_reachable()

        # Add static target label as reachable in case we are looking
        # for reachable labels from a single label, instead of all labels.
        if not self.expression:
            target = renpy.game.script.lookup_or_none(self.label)
            if target is not None:
                rv = [target, *rv]

        return rv


class Return(Node):

    expression: str | None = None

    def __init__(self, loc: tuple[str, int], expression: str | None = None):
        super(Return, self).__init__(loc)
        self.expression = expression

    @override
    def diff_info(self):
        return (Return,)

    # We don't care what the next node is.
    @override
    def chain(self, next: Node | None):
        self.next: Node | None = None

    @override
    def execute(self):

        statement_name("return")

        if self.expression:
            renpy.store._return = renpy.python.py_eval(self.expression)
        else:
            renpy.store._return = None

        ctx = renpy.game.context()

        if renpy.game.context().init_phase:
            if len(ctx.return_stack) == 0:

                if renpy.config.developer:
                    raise Exception("Unexpected return during the init phase.")

                return

        next_node(renpy.game.context().lookup_return(pop=True))
        renpy.game.context().pop_dynamic()

    @override
    def predict(self):

        return [renpy.game.context().predict_return()]

    @override
    def scry(self):
        rv = super().scry()
        rv._next = None  # pyright: ignore[reportPrivateUsage]
        return rv


class Menu(Node):

    translation_relevant: bool = True

    items: list[tuple[str, str, list[Node] | None]]
    statement_start: Node  # type: ignore
    set: str | None = None
    with_: str | None = None
    has_caption: bool = False
    arguments: ArgumentInfo | None = None
    item_arguments: list[ArgumentInfo | None] | None = None
    rollback: RollbackType = "force"  # type: ignore

    def __init__(
        self,
        loc: tuple[str, int],
        items: list[tuple[str, str, list[Node] | None]],
        set: str | None = None,
        with_: str | None = None,
        has_caption: bool = False,
        arguments: ArgumentInfo | None = None,
        item_arguments: list[ArgumentInfo | None] | None = None,
    ):
        super(Menu, self).__init__(loc)

        self.items = items
        self.set = set
        self.with_ = with_
        self.has_caption = has_caption
        self.arguments = arguments
        self.item_arguments = item_arguments

    @override
    def diff_info(self):
        return (Menu,)

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for _label, _condition, block in self.items:
            if block:
                for i in block:
                    i.get_children(f)

    # Blocks of statements in a choice continue after the menu.
    @override
    def chain(self, next: Node | None):

        self.next: Node | None = next

        for _label, _condition, block in self.items:
            if block:
                chain_block(block, next)

    @override
    def replace_next(self, old: Node, new: Node):
        super().replace_next(old, new)

        for _label, _condition, block in self.items:
            if block and (block[0] is old):
                block.insert(0, new)

    @override
    def execute(self):

        next_node(self.next)

        if self.arguments is not None:
            args, kwargs = self.arguments.evaluate()
        else:
            args = kwargs = None

        name = "menu"

        if kwargs is not None and kwargs.get("nvl") is True:
            name = "menu-nvl"

        if self.has_caption or renpy.config.choice_empty_window:
            name += "-with-caption"

        statement_name(name)

        choices: list[tuple[str, str, int | None]] = []
        narration: list[str] = []
        item_arguments = []

        for i, (label, condition, block) in enumerate(self.items):

            if renpy.config.say_menu_text_filter:
                label = renpy.config.say_menu_text_filter(label)

            has_item = False

            if block is None:
                if renpy.config.narrator_menu and label:
                    narration.append(label)
                else:
                    choices.append((label, condition, None))
                    has_item = True

            else:
                choices.append((label, condition, i))
                has_item = True

                next_node(block[0])

            if has_item:
                if self.item_arguments and (self.item_arguments[i] is not None):
                    item_arguments.append(self.item_arguments[i].evaluate())
                else:
                    item_arguments.append(((), {}))

        if narration:
            renpy.exports.say(None, "\n".join(narration), interact=False)

        say_menu_with(self.with_, renpy.game.interface.set_transition)

        choice = renpy.exports.menu(choices, self.set, args, kwargs, item_arguments)

        if choice is not None:
            next_node(self.items[choice][2][0])
        else:
            next_node(self.next)

    @override
    def predict(self):
        rv = []

        def predict_with(trans):
            renpy.display.predict.displayable(trans(old_widget=None, new_widget=None))

        say_menu_with(self.with_, predict_with)

        renpy.store.predict_menu()

        for _label, _condition, block in self.items:
            if block:
                rv.append(block[0])

        return rv

    @override
    def scry(self):
        rv = super().scry()
        rv._next = None  # pyright: ignore[reportPrivateUsage]
        rv.interacts = True
        if self.has_caption:
            rv.menu_with_caption = True
        return rv

    @override
    def restructure(self, callback):
        for _label, _condition, block in self.items:
            if block is not None:
                callback(block)

    @override
    def get_reachable(self):
        rv: list[Node] = []

        for _, _, block in self.items:
            if not block:
                continue

            rv.append(block[0])

        # Technically, if all choices ends with a jump or return, self.next
        # is unreachable, but self.set and empty menu with conditions, or
        # even custom display_menu realizations, will fall through to
        # self.next, so we need to add it here, to not falsely report that
        # self.next is unreachable.
        if self.next is not None:
            rv.append(self.next)

        return rv

    @override
    def get_translation_strings(self):
        rv = super().get_translation_strings()

        for caption, _, block in self.items:
            if renpy.config.old_substitutions:
                caption = caption.replace("%%", "%")

            if caption is None:
                continue

            # Empty lines after the caption will strill make
            # this caption to be repoprted on wrong line,
            # but it is still better than line number of the menu itself
            # which can be hundreds of lines away.
            if block:
                loc = block[0].linenumber - 1
            else:
                loc = self.linenumber

            rv.append((loc, caption))

        return rv


setattr(Menu, "with", Menu.with_)  # type: ignore


# Goto is considered harmful. So we decided to name it "jump"
# instead.
class Jump(Node):

    target: str
    expression: bool = False
    global_label: str = ""

    def __init__(
        self,
        loc: tuple[str, int],
        target: str,
        expression: bool,
        global_label: str = "",
    ):
        super(Jump, self).__init__(loc)

        self.target = target
        self.expression = expression
        self.global_label = global_label

    @override
    def diff_info(self):
        return (Jump, self.target, self.expression)

    # We don't care what our next node is.
    @override
    def chain(self, next: Node | None):
        self.next: Node | None = None

    @override
    def execute(self):

        statement_name("jump")

        target = self.target
        if self.expression:
            target = renpy.python.py_eval(target)

            if isinstance(target, str) and target.startswith("."):
                target = self.global_label + target

        rv = renpy.game.script.lookup(target)
        renpy.game.context().abnormal = True

        next_node(rv)

    @override
    def predict(self):

        label = self.target

        if self.expression:

            if not probably_side_effect_free(label):
                return []

            try:
                label = renpy.python.py_eval(label)
            except Exception:
                return []

            if isinstance(label, str) and label.startswith("."):
                label = self.global_label + label

            if not renpy.game.script.has_label(label):
                return []

        return [renpy.game.script.lookup(label)]

    @override
    def scry(self):
        rv = super().scry()
        if self.expression:
            rv._next = None
        else:
            rv._next = renpy.game.script.lookup(self.target)

        return rv

    @override
    def get_reachable(self) -> list[Node]:
        rv = super().get_reachable()

        # Add static target label as reachable in case we are looking
        # for reachable labels from a single label, instead of all labels.
        if not self.expression:
            target = renpy.game.script.lookup_or_none(self.target)
            if target is not None:
                rv = [target, *rv]

        return cast(list[Node], rv)


# GNDN
class Pass(Node):
    @override
    def diff_info(self):
        return (Pass,)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("pass")


class While(Node):

    condition: str
    block: list[Node]

    def __init__(self, loc: tuple[str, int], condition: str, block: list[Node]):
        super(While, self).__init__(loc)

        self.condition = condition
        self.block = block

    @override
    def diff_info(self):
        return (While, self.condition)

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for i in self.block:
            i.get_children(f)

    @override
    def chain(self, next):
        self.next = next
        chain_block(self.block, self)

    @override
    def replace_next(self, old: Node, new: Node):
        super().replace_next(old, new)

        if self.block and (self.block[0] is old):
            self.block.insert(0, new)

    @override
    def execute(self):

        next_node(self.next)
        statement_name("while")

        if renpy.python.py_eval(self.condition):
            next_node(self.block[0])

    @override
    def predict(self):
        return [self.block[0], self.next]

    @override
    def scry(self):
        rv = super().scry()
        rv._next = None  # pyright: ignore[reportPrivateUsage]
        return rv

    @override
    def restructure(self, callback: Callable[[list[Node]], Any]):
        callback(self.block)

    @override
    def get_reachable(self):
        if self.condition == "True":
            return [self.block[0]]
        else:
            return [self.block[0], *super().get_reachable()]


class If(Node):

    entries: list[tuple[str, list[Node]]]

    def __init__(self, loc: tuple[str, int], entries: list[tuple[str, list[Node]]]):
        """
        @param entries: A list of (condition, block) tuples.
        """

        super(If, self).__init__(loc)

        self.entries = entries

    @override
    def diff_info(self):
        return (If,)

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for _condition, block in self.entries:
            for i in block:
                i.get_children(f)

    @override
    def chain(self, next: Node | None):
        self.next: Node | None = next

        for _condition, block in self.entries:
            chain_block(block, next)

    @override
    def replace_next(self, old: Node, new: Node):
        super().replace_next(old, new)

        for _condition, block in self.entries:
            if (block) and (block[0] is old):
                block.insert(0, new)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("if")

        for condition, block in self.entries:
            if renpy.python.py_eval(condition):
                next_node(block[0])
                return

    @override
    def predict(self):
        return [block[0] for _condition, block in self.entries] + [self.next]

    @override
    def scry(self):
        rv = super().scry()
        rv._next = None  # pyright: ignore[reportPrivateUsage]
        return rv

    @override
    def restructure(self, callback):
        for _condition, block in self.entries:
            callback(block)

    @override
    def get_reachable(self):
        rv: list[Node] = []

        # First branch that is always True (like else branch)
        # can make self.next unreachable unless the block
        # can reach it.
        for condition, block in self.entries:
            rv.append(block[0])
            if condition == "True":
                return rv

        return [*rv, *super().get_reachable()]


class UserStatement(Node):

    line: str
    parsed: Any
    block: list[Any] = []
    translatable: bool = False  # type: ignore
    code_block: list[Node] | None = None
    translation_relevant: bool = False  # type: ignore
    rollback: RollbackType = "normal"
    subparses: list["renpy.lexer.SubParse"] = []
    init_priority: SignedInt = 0
    atl: "renpy.atl.RawBlock | None" = None

    def __init__(self, loc: tuple[str, int], line: str, block: list[Any], parsed: Any):

        super(UserStatement, self).__init__(loc)
        self.code_block = None
        self.parsed = parsed
        self.line = line
        self.block = block
        self.subparses = []
        self.init_priority = 0

        self.name = self.call("label")
        self.rollback = renpy.statements.get("rollback", self.parsed) or "normal"  # type: ignore

    @override
    def __repr__(self):
        return "<UserStatement {!r}>".format(self.line)

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        if self.code_block is not None:
            for i in self.code_block:
                i.get_children(f)

        for i in self.subparses:
            for j in i.block:
                j.get_children(f)

    @override
    def chain(self, next: Node | None):
        self.next: Node | None = next

        if self.code_block is not None:
            chain_block(self.code_block, next)

        for i in self.subparses:
            chain_block(i.block, next)

    @override
    def replace_next(self, old: Node, new: Node):
        super().replace_next(old, new)

        if (self.code_block) and (self.code_block[0] is old):
            self.code_block.insert(0, new)

        for i in self.subparses:
            if i.block[0] is old:
                i.block.insert(0, new)

    @override
    def restructure(self, callback: Callable[[list[Node]], Any]):
        if self.code_block:
            callback(self.code_block)

        for i in self.subparses:
            callback(i.block)

    @override
    def diff_info(self):
        return (UserStatement, self.line)

    def call(self, method, *args, **kwargs):

        parsed = self.parsed

        if parsed is None:
            parsed = renpy.statements.parse(self, self.line, self.block)
            self.parsed = parsed

        return renpy.statements.call(method, parsed, *args, **kwargs)

    @override
    def execute_init(self):
        self.call("execute_init")

        if renpy.statements.get("init", self.parsed):
            self.init_priority = 1

        if renpy.statements.get("execute_default", self.parsed):
            default_statements.append(self)

    def get_init(self):
        return self.init_priority

    @override
    def execute(self):
        next_node(self.get_next())
        statement_name(self.get_name())

        if self.atl is not None:
            self.call("execute", atl=renpy.display.transform.ATLTransform(self.atl))
        else:
            self.call("execute")

    def execute_default(self, start):
        self.call("execute_default")

    @override
    def predict(self):
        predictions = self.call("predict")

        if predictions is not None:
            for i in predictions:
                renpy.easy.predict(i)

        if self.atl is not None:
            renpy.display.predict.displayable(
                renpy.display.transform.ATLTransform(self.atl)
            )

        if self.parsed and renpy.statements.get("predict_all", self.parsed):
            return [i.block[0] for i in self.subparses] + [self.next]

        if self.next:
            next_label = self.next.name
        else:
            next_label = None

        next_list = self.call("predict_next", next_label)

        if next_list is not None:
            nexts = [
                renpy.game.script.lookup_or_none(i) for i in next_list if i is not None
            ]
            return [i for i in nexts if i is not None]

        return [self.next]

    def get_name(self):
        parsed = self.parsed

        if parsed is None:
            parsed = renpy.statements.parse(self, self.line, self.block)
            self.parsed = parsed

        return renpy.statements.get_name(parsed)

    def get_next(self):

        if self.code_block and len(self.code_block):
            rv = self.call("next", self.code_block[0].name)
        else:
            rv = self.call("next")

        if rv is not None:
            return renpy.game.script.lookup(rv)
        else:
            return self.next

    @override
    def scry(self):
        rv = super().scry()
        rv._next = self.get_next()
        self.call("scry", rv)
        return rv

    @override
    def get_code(self, dialogue_filter: Callable[[str], str] = None):
        return self.line

    @override
    def can_warp(self):

        if self.call("warp"):
            return True

        return False

    def reachable(
        self, is_reachable: bool
    ) -> set[NodeName | Literal[True] | "renpy.lexer.SubParse" | None]:
        """
        This is used by lint to find statements reachable from or through
        this statement.
        """

        rv = self.call(
            "reachable",
            is_reachable,
            self.name,
            self.next.name if self.next is not None else None,
            self.code_block[0].name if self.code_block else None,
        )

        if rv is None:

            rv = set()

            if self.call("label"):
                rv.add(self.name)
                is_reachable = True

            if is_reachable:

                if self.code_block:
                    rv.add(self.code_block[0].name)

                for i in self.subparses:
                    if i.block:
                        rv.add(i.block[0].name)

                if self.next:
                    rv.add(self.next.name)

        return rv

    @override
    def get_reachable(self) -> list[Node]:
        reachable = self.reachable(True)

        rv = list()
        for name in reachable:
            if name is None:
                continue

            if name is True:
                continue

            if isinstance(name, renpy.lexer.SubParse):
                if name.block:
                    rv.append(name.block[0])
                continue

            node = renpy.game.script.lookup(name)

            if node is None:
                continue

            rv.append(node)

        return rv

    def analyze(self):
        if self.atl is not None:
            self.atl.analyze(EMPTY_PARAMETERS)

    @override
    def get_translation_strings(self) -> list[tuple[int, str]]:
        rv = super().get_translation_strings()

        if strings := self.call("translation_strings"):
            for i in strings:
                if not isinstance(i, tuple):
                    i = (self.linenumber, i)

                rv.append(i)

        return rv


class PostUserStatement(Node):

    parent: UserStatement

    def __init__(self, loc: tuple[str, int], parent):

        super(PostUserStatement, self).__init__(loc)
        self.parent = parent

        self.name = self.parent.call("post_label")

    @override
    def __repr__(self):
        return "<PostUserStatement {!r}>".format(self.parent.line)

    @override
    def diff_info(self):
        return (PostUserStatement, self.parent.line)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("post " + self.parent.get_name())

        self.parent.call("post_execute")


# All the define statements, in the order they were registered.
define_statements: list["Define"] = []


class Define(Node):

    varname: str
    code: PyCode
    store: str = "store"
    operator: str = "="
    index: PyCode | None = None

    def __init__(
        self,
        loc: tuple[str, int],
        store: str,
        name: str,
        index: str | None,
        operator: str,
        expr: str,
    ):
        super(Define, self).__init__(loc)

        self.store = store
        self.varname = name

        if index is not None:
            self.index = PyCode(index, loc=loc, mode="eval")
        else:
            self.index = None

        self.operator = operator
        self.code = PyCode(expr, loc=loc, mode="eval")

    @override
    def diff_info(self):
        return (Define, self.store, self.varname)

    @override
    def early_execute(self):
        create_store(self.store)

        if self.operator != "=":
            return

        if self.index is not None:
            return

        if self.store == "store.config" and self.varname in EARLY_CONFIG:
            value = renpy.python.py_eval_bytecode(self.code.bytecode)
            setattr(renpy.config, self.varname, value)

    @override
    def execute(self):

        next_node(self.next)
        statement_name("define")

        define_statements.append(self)

        if self.store == "store":
            renpy.dump.definitions.append(
                (self.varname, self.filename, self.linenumber)
            )
        else:
            renpy.dump.definitions.append(
                (self.store[6:] + "." + self.varname, self.filename, self.linenumber)
            )

        if self.operator == "=" and self.index is None:
            ns, _special = get_namespace(self.store)
            if getattr(ns, "pure", True):
                renpy.exports.pure(self.store + "." + self.varname)

        self.set()

    def redefine(self, stores: list[str]):

        if self.store not in stores:
            return

        self.set()

    def set(self):

        key = None
        new = None

        value = renpy.python.py_eval_bytecode(self.code.bytecode)
        ns, _special = get_namespace(self.store)

        if (self.index is None) and (self.operator == "="):
            ns.set(self.varname, value)
            return

        base = ns.get(self.varname)
        old = base

        if self.index:
            key = renpy.python.py_eval_bytecode(self.index.bytecode)

            if self.operator != "=":
                old = base[key]

        if self.operator == "=":
            new = value
        elif self.operator == "+=":
            new = old + value
        elif self.operator == "|=":
            new = old | value

        if self.index:
            base[key] = new
        else:
            ns.set(self.varname, new)


# All the default statements, in the order they were registered.
default_statements: list[Default | UserStatement] = []


class Default(Node):

    varname: str
    code: PyCode
    store: str = "store"

    def __init__(self, loc: tuple[str, int], store: str, name: str, expr: str):

        super(Default, self).__init__(loc)

        self.store = store
        self.varname = name
        self.code = PyCode(expr, loc=loc, mode="eval")

    @override
    def diff_info(self):
        return (Default, self.store, self.varname)

    @override
    def early_execute(self):
        create_store(self.store)

    @override
    def execute(self):

        next_node(self.next)
        statement_name("default")

        ns, special = get_namespace(self.store)

        if special:
            value = renpy.python.py_eval_bytecode(self.code.bytecode)
            ns.set_default(self.varname, value)

            if getattr(ns, "repeat_at_default_time", False):
                default_statements.append(self)

            return

        default_statements.append(self)

        if self.store == "store":
            renpy.dump.definitions.append(
                (self.varname, self.filename, self.linenumber)
            )
        else:
            renpy.dump.definitions.append(
                (self.store[6:] + "." + self.varname, self.filename, self.linenumber)
            )

    def execute_default(self, start):

        # Handle special namespaces.
        ns, special = get_namespace(self.store)

        if special:
            value = renpy.python.py_eval_bytecode(self.code.bytecode)
            ns.set_default(self.varname, value)
            return

        # Handle normal namespaces.
        d = renpy.python.store_dicts[self.store]

        defaults_set = d.get("_defaults_set", None)

        if defaults_set is None:
            d["_defaults_set"] = defaults_set = renpy.revertable.RevertableSet()
            d.ever_been_changed.add("_defaults_set")

        if self.varname in defaults_set:
            if start and renpy.config.developer:
                raise Exception(
                    "{}.{} is being given a default a second time.".format(
                        self.store, self.varname
                    )
                )
            return

        # do the variable shadowing if not in this case, for compatibility reasons
        if start and (renpy.config.developer is True):
            fullname = ".".join((self.store, self.varname))
            if fullname in renpy.python.store_dicts:
                raise Exception(
                    "{} is being given a default, but a store with that name already exists.".format(
                        fullname
                    )
                )

        if start or (self.varname not in d.ever_been_changed):
            d[self.varname] = renpy.python.py_eval_bytecode(self.code.bytecode)

        d.ever_been_changed.add(self.varname)

        defaults_set.add(self.varname)

    def report_traceback(self, name, last):
        return [(self.filename, self.linenumber, name, None)]


class Screen(Node):

    screen: renpy.sl2.slast.SLScreen

    def __init__(self, loc: tuple[str, int], screen: renpy.sl2.slast.SLScreen):
        """
        @param screen: The screen object being defined.
        In SL1, an instance of screenlang.ScreenLangScreen.
        In SL2, an instance of sl2.slast.SLScreen.
        """

        super(Screen, self).__init__(loc)

        self.screen = screen

    @override
    def diff_info(self):
        return (Screen, self.screen.name)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("screen")

        self.screen.define((self.filename, self.linenumber))
        renpy.dump.screens.append((self.screen.name, self.filename, self.linenumber))


class Style(Node):

    style_name: str
    parent: str | None
    properties: dict[str, str]
    clear: bool
    take: str | None
    delattr: list[str]
    variant: str | None

    def __init__(self, loc: tuple[str, int], name: str):
        """
        `name`
            The name of the style to define.
        """

        super(Style, self).__init__(loc)

        self.style_name = name

        # The parent of this style.
        self.parent = None

        # Properties.
        self.properties = {}

        # Should we clear the style?
        self.clear = False

        # Should we take properties from another style?
        self.take = None

        # A list of attributes we should delete from this style.
        self.delattr = []

        # If not none, an expression for the variant.
        self.variant = None

    @override
    def diff_info(self):
        return (Style, self.style_name)

    def apply(self):
        if self.variant is not None:
            variant = renpy.python.py_eval(self.variant)
            if not renpy.exports.variant(variant):
                return

        s = renpy.style.get_or_create_style(self.style_name)  # type: ignore

        if self.clear:
            s.clear()

        if self.parent is not None:
            s.set_parent(self.parent)

        if self.take is not None:
            s.take(self.take)

        for i in self.delattr:
            s.delattr(i)

        if self.properties:
            properties = {}

            for name, expr in self.properties.items():

                value = renpy.python.py_eval(expr)

                if name == "properties":
                    properties.update(value)
                else:
                    properties[name] = value

            s.add_properties(properties)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("style")

        if renpy.config.defer_styles and renpy.game.context().init_phase:
            renpy.translation.deferred_styles.append(self)
            return

        self.apply()


class Testcase(Node):

    label: str
    test: renpy.test.testast.Block

    def __init__(
        self,
        loc: tuple[str, int],
        label: str,
        test: renpy.test.testast.Block,
    ):
        super(Testcase, self).__init__(loc)

        self.label = label
        self.test = test

    @override
    def diff_info(self):
        return (Testcase, self.label)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("testcase")

        renpy.test.testexecution.testcases[self.label] = self.test


class RPY(Node):
    rest: tuple[str, ...]

    def __init__(self, loc: tuple[str, int], rest: tuple[str, ...]):
        super(RPY, self).__init__(loc)

        self.rest = rest

    @override
    def diff_info(self):
        return (RPY, self.rest)

    @override
    def execute(self):
        next_node(self.next)
        statement_name("rpy")

        # rpy python is run in Script.finish_load.

    @override
    def get_code(self, dialogue_filter: Callable[[str], str] | None = None):
        return "rpy " + " ".join(self.rest)


################################################################################
# Translations
################################################################################


class Translate(Node):
    """
    A translation block, produced either by explicit translation statements
    or implicit translation blocks.

    If language is None, when executed this transfers control to the translate
    statement in the current language, if any, and otherwise runs the block.
    If language is not None, causes an error to occur if control reaches this
    statement.

    When control normally leaves a translate statement, in any language, it
    goes to the end of the translate statement in the None language.
    """

    rollback: RollbackType = "never"
    translation_relevant: bool = True

    identifier: str
    alternate: str | None
    language: str | None
    block: list[Node]
    after: Node | None

    def __init__(
        self,
        loc: tuple[str, int],
        identifier: str,
        language: str | None,
        block: list[Node],
        alternate: str | None = None,
    ):
        super(Translate, self).__init__(loc)

        self.identifier = identifier
        self.alternate = alternate
        self.language = language
        self.block = block

    @override
    def diff_info(self):
        return (Translate, self.identifier, self.language)

    @override
    def chain(self, next: Node | None):
        if self.block:
            self.next: Node | None = self.block[0]
            chain_block(self.block, next)
        else:
            self.next = next

        self.after = next

    @override
    def replace_next(self, old: Node, new: Node):
        super().replace_next(old, new)

        if self.block and (self.block[0] is old):
            self.block.insert(0, new)

        if self.after is old:
            self.after = new

    def lookup(self) -> Node:
        return renpy.game.script.translator.lookup_translate(
            self.identifier, getattr(self, "alternate", None)
        )

    @override
    def execute(self):

        if self.language is not None:
            next_node(self.next)
            raise Exception("Translation nodes cannot be run directly.")

        next_node(self.lookup())

        renpy.game.context().translate_identifier = self.identifier
        renpy.game.context().alternate_translate_identifier = getattr(
            self, "alternate", None
        )

    @override
    def predict(self) -> list[Node | None]:
        return [self.lookup()]

    @override
    def scry(self):
        rv = Scry()
        rv._next = self.lookup()
        return rv

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for i in self.block:
            i.get_children(f)

    @override
    def restructure(self, callback: Callable[[list[Node]], None]):
        return callback(self.block)

    @override
    def get_reachable(self):
        rv = super().get_reachable()
        # Language translates are not really reachable,
        # so this branch is only for cases we check for
        # e.g. orpahn translations.
        if self.language is not None:
            return rv

        nodes = renpy.game.script.translator.get_all_translates(self.identifier)

        rv += [n.block[0] for n in nodes.values() if n.block]
        return rv


class TranslateSay(Say):
    """
    A node that combines a translate and a say statement.
    """

    translatable: bool = True
    translation_relevant: bool = True

    alternate: str | None
    language: str | None

    @property
    def after(self):
        return self.next

    @property
    def block(self) -> list[Node]:
        return []

    def __init__(
        self,
        loc: tuple[str, int],
        who: str | None,
        what: str,
        with_: str | None,
        interact: bool = True,
        attributes: tuple[str, ...] | None = None,
        arguments: ArgumentInfo | None = None,
        temporary_attributes: tuple[str, ...] | None = None,
        identifier: str | None = None,
        language: str | None = None,
        alternate: str | None = None,
    ):
        super(TranslateSay, self).__init__(
            loc,
            who,
            what,
            with_,
            interact,
            attributes,
            arguments,
            temporary_attributes,
            identifier,
        )

        self.identifier = identifier
        self.alternate = alternate
        self.language = language

        self.identifier: str | None = identifier
        self.alternate = alternate
        self.language = language

    @override
    def diff_info(self):
        if self.language is None:
            return Say.diff_info(self)
        else:
            return (TranslateSay, self.identifier, self.language)

    @override
    def chain(self, next: Node | None):
        Say.chain(self, next)

    @override
    def replace_next(self, old: Node, new: Node):
        Say.replace_next(self, old, new)

    def lookup(self) -> Node:
        return renpy.game.script.translator.lookup_translate(
            self.identifier, getattr(self, "alternate", None)
        )

    @override
    def execute(self):

        next_node(self.next)

        renpy.game.context().translate_identifier = self.identifier
        renpy.game.context().alternate_translate_identifier = getattr(
            self, "alternate", None
        )

        if self.language is None:

            # Potentially, jump to a translation.
            node = self.lookup()

            if (node is not None) and (node is not self):
                next_node(node)
                return

        # Otherwise, say the text.

        try:

            Say.execute(self)

        finally:

            hashed_key = renpy.astsupport.hash64(self.identifier)

            if (self.identifier not in renpy.game.persistent._seen_translates) and (
                hashed_key not in renpy.game.persistent._seen_translates
            ):

                if renpy.config.hash_seen:
                    renpy.game.persistent._seen_translates.add(hashed_key)
                else:
                    renpy.game.persistent._seen_translates.add(self.identifier)

                renpy.game.seen_translates_count += 1
                renpy.game.new_translates_count += 1

            # Perform the equivalent of an endtranslate block.
            renpy.game.context().translate_identifier = None
            renpy.game.context().alternate_translate_identifier = None

    @override
    def predict(self) -> list[Node | None]:
        node = self.lookup()

        if node is None or node is self:
            return Say.predict(self)

        return [node]

    @override
    def scry(self):
        node = self.lookup()

        if node is None or node is self:
            return Say.scry(self)

        rv = Scry()
        rv._next = self.next  # pyright: ignore[reportPrivateUsage]

        return rv

    @override
    def get_reachable(self):
        rv = super().get_reachable()
        # Language translates are not really reachable,
        # so this branch is only for cases we check for
        # e.g. orpahn translations.
        if self.language is not None:
            return rv

        assert self.identifier is not None
        nodes = renpy.game.script.translator.get_all_translates(self.identifier)

        rv += [n.block[0] for n in nodes.values() if n.block]
        return rv


class EndTranslate(Node):
    """
    A node added implicitly after each translate block. It's responsible for
    resetting the translation identifier.
    """

    rollback: RollbackType = "never"

    @override
    def diff_info(self):
        return (EndTranslate,)

    @override
    def execute(self):
        next_node(self.next)

        tlid = renpy.game.context().translate_identifier
        if tlid is not None:

            hashed_key = renpy.astsupport.hash64(tlid)

            if (tlid not in renpy.game.persistent._seen_translates) and (
                hashed_key not in renpy.game.persistent._seen_translates
            ):

                if renpy.config.hash_seen:
                    renpy.game.persistent._seen_translates.add(hashed_key)
                else:
                    renpy.game.persistent._seen_translates.add(tlid)

                renpy.game.seen_translates_count += 1
                renpy.game.new_translates_count += 1

        renpy.game.context().translate_identifier = None
        renpy.game.context().alternate_translate_identifier = None


class TranslateString(Node):
    """
    A node used for translated strings.
    """

    translation_relevant: bool = True

    language: str
    old: str
    new: str
    newloc: tuple[str, int]

    def __init__(
        self,
        loc: tuple[str, int],
        language: str,
        old: str,
        new: str,
        newloc: tuple[str, int],
    ):
        super(TranslateString, self).__init__(loc)
        self.language = language

        self.old = old
        self.new = new
        self.newloc = newloc

    @override
    def diff_info(self):
        return (TranslateString,)

    @override
    def execute(self):
        next_node(self.next)

        newloc = getattr(self, "newloc", (self.filename, self.linenumber + 1))
        renpy.translation.add_string_translation(
            self.language, self.old, self.new, newloc
        )


class TranslatePython(Node):
    """
    Runs python code when changing the language.

    This is no longer generated, but is still run when encountered.
    """

    translation_relevant: bool = True

    language: str
    code: PyCode

    def __init__(self, loc: tuple[str, int], language: str, python_code: str):
        """
        @param code: A PyCode object.

        @param hide: If True, the code will be executed with its
        own local dictionary.
        """

        super(TranslatePython, self).__init__(loc)

        self.language = language
        self.code = PyCode(python_code, loc=loc, mode="exec")

    @override
    def diff_info(self):
        return (TranslatePython, self.code.source)

    @override
    def execute(self):
        next_node(self.next)

    # def early_execute(self):
    #    renpy.python.create_store(self.store)
    #    renpy.python.py_exec_bytecode(self.code.bytecode, self.hide, store=self.store)


class TranslateBlock(Node):
    """
    Runs a block of code when changing the language.
    """

    translation_relevant: bool = True

    block: list[Node]
    language: str

    def __init__(self, loc: tuple[str, int], language: str, block: list[Node]):
        super(TranslateBlock, self).__init__(loc)

        self.language = language
        self.block = block

    @override
    def get_children(self, f: Callable[[Node], Any]):
        f(self)

        for i in self.block:
            i.get_children(f)

    # We handle chaining specially. We want to chain together the nodes in
    # the block, but we want that chain to end in None, and we also want
    # this node to just continue on to the next node in normal execution.
    @override
    def chain(self, next: Node | None):
        self.next: Node | None = next
        chain_block(self.block, None)

    @override
    def execute(self):
        next_node(self.next)

    @override
    def restructure(self, callback: Callable[[list[Node]], None]):
        callback(self.block)


class TranslateEarlyBlock(TranslateBlock):
    """
    This is similar to the TranslateBlock, except it runs before deferred
    styles do.
    """
