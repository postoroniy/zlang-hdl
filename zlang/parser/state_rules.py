# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded stateless Lark callbacks owned by StateRules."""

from __future__ import annotations

import lark
from lark import v_args

from zlang.ast import nodes as ast_nodes
from zlang.parser.errors import ParseError

from .rules_support import _ParsedRuleTarget, _tagged


class StateRules:
    """Stateless grammar callbacks for one bounded parser domain."""

    @v_args(meta=True)
    def external_module_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.Module:
        name = str(items[0])
        interface_ref = items[1]
        return ast_nodes.Module(
            name=name,
            ports=(),
            assignments=(),
            ordered_items=(),
            conforms_to=interface_ref,
            external_model=str(items[2]),
            external_origin=self._span(meta),
            name_origin=self._token_span(items[0]),
        )

    def module_timing_latency(self, items: list[object]) -> tuple[str, int]:
        return ("module_timing_latency", self._parse_number(items[0]))

    def module_timing_ii(self, items: list[object]) -> tuple[str, int]:
        return ("module_timing_ii", self._parse_number(items[0]))

    @v_args(meta=True)
    def module_timing_decl(
        self,
        meta: object,
        items: list[object],
    ) -> ast_nodes.ModuleTimingDecl:
        latencies = tuple(
            item[1] for item in items if _tagged(item, "module_timing_latency")
        )
        intervals = tuple(
            item[1] for item in items if _tagged(item, "module_timing_ii")
        )
        if len(latencies) != 1 or len(intervals) != 1:
            raise ParseError(
                "timing block requires exactly one latency directive and "
                "exactly one ii directive"
            )
        return ast_nodes.ModuleTimingDecl(
            latency=latencies[0],
            initiation_interval=intervals[0],
            origin=self._span(meta),
        )

    def clock_physical_block(self, items: list[object]) -> tuple[str, str]:
        return ("clock_physical", str(items[0]))

    @v_args(meta=True)
    def clock_decl(self, meta: object, items: list[object]) -> tuple[object, ...]:
        edge = (
            str(items[1][1])
            if len(items) > 1 and items[1] is not None
            else "rising"
        )
        if edge not in {"rising", "falling"}:
            raise ParseError("clock edge must be 'rising' or 'falling'")
        declaration = ast_nodes.ClockPhysicalDecl(str(items[0]), edge, self._span(meta))
        return ("clock", declaration.name, declaration)

    def reset_physical_block(self, items: list[object]) -> tuple[str, str, str, str]:
        return ("reset_physical", *(str(item) for item in items))

    def async_reset_polarity(self, items: list[object]) -> tuple[str, str]:
        return ("polarity", str(items[0]))

    def async_reset_release(self, items: list[object]) -> tuple[str, str]:
        return ("release", str(items[0]))

    def async_reset_physical_block(
        self, items: list[object]
    ) -> tuple[object, ...]:
        values: dict[str, str] = {}
        for item in items:
            assert isinstance(item, tuple)
            key, value = str(item[0]), str(item[1])
            if key in values:
                raise ParseError(f"async reset {key} may be specified only once")
            values[key] = value
        return ("async_reset_physical", values)

    @v_args(meta=True)
    def reset_decl(self, meta: object, items: list[object]) -> tuple[object, ...]:
        name = str(items[0])
        domain = next(
            (
                str(item)
                for item in items[1:]
                if item is not None and not isinstance(item, tuple)
            ),
            None,
        )
        block = next(
            (item for item in items[1:] if _tagged(item, "reset_physical")),
            None,
        )
        mode, polarity, power_up = (
            ("synchronous", "active_high", "unspecified")
            if block is None
            else (str(block[1]), str(block[2]), str(block[3]))
        )
        if mode not in {"synchronous", "asynchronous"}:
            raise ParseError(
                "reset mode must be 'synchronous' or 'asynchronous'"
            )
        if polarity not in {"active_high", "active_low"}:
            raise ParseError(
                "reset polarity must be 'active_high' or 'active_low'"
            )
        if power_up not in {"unspecified", "reset"}:
            raise ParseError("reset power_up must be 'unspecified' or 'reset'")
        declaration = ast_nodes.ResetPhysicalDecl(
            name, domain, mode, polarity, power_up, self._span(meta)
        )
        return ("reset", name, domain, declaration)

    @v_args(meta=True)
    def async_reset_decl(
        self, meta: object, items: list[object]
    ) -> tuple[object, ...]:
        name = str(items[0])
        domain = next(
            (
                str(item)
                for item in items[1:]
                if item is not None and not isinstance(item, tuple)
            ),
            None,
        )
        block = next(
            (
                item for item in items[1:]
                if _tagged(item, "async_reset_physical")
            ),
            None,
        )
        attributes = {} if block is None else block[1]
        assert isinstance(attributes, dict)
        polarity = str(attributes.get("polarity", "active_high"))
        if polarity not in {"active_high", "active_low"}:
            raise ParseError(
                "reset polarity must be 'active_high' or 'active_low'"
            )
        release = str(attributes.get("release", "synchronized"))
        if release not in {"synchronized", "externally_synchronized"}:
            raise ParseError(
                "async reset release must be 'synchronized' or "
                "'externally_synchronized'"
            )
        declaration = ast_nodes.ResetPhysicalDecl(
            name=name,
            clock=domain,
            mode="asynchronous",
            polarity=polarity,
            power_up="unspecified",
            origin=self._span(meta),
            release_mode=release,
            release_cycles=2 if release == "synchronized" else 0,
        )
        return ("reset", name, domain, declaration)

    @v_args(meta=True)
    def register_decl(self, meta: object, items: list[object]) -> ast_nodes.RegisterDecl:
        tail = tuple(item for item in items[2:] if item is not None)
        domain = next(
            (str(item) for item in tail if isinstance(item, lark.Token)),
            None,
        )
        initial = next(
            (item for item in tail if not isinstance(item, lark.Token)),
            None,
        )
        return ast_nodes.RegisterDecl(
            str(items[0]),
            items[1],
            initial,
            domain,
            self._span(meta),
            self._token_span(items[0]),
        )

    @v_args(meta=True)
    def next_assignment(self, meta: object, items: list[object]) -> ast_nodes.NextAssignment:
        return ast_nodes.NextAssignment(
            str(items[0]),
            items[1],
            self._token_span(items[0]) or self._name_span_from_meta(
                meta, str(items[0])
            ),
        )

    @v_args(meta=True)
    def scalar_rule_target(
        self, meta: object, items: list[object]
    ) -> _ParsedRuleTarget:
        return _ParsedRuleTarget(
            str(items[0]),
            self._token_span(items[0]) or self._name_span_from_meta(
                meta, str(items[0])
            ),
        )

    @v_args(meta=True)
    def indexed_rule_target(
        self,
        meta: object,
        items: list[object],
    ) -> ast_nodes.IndexedAssignmentTarget:
        return ast_nodes.IndexedAssignmentTarget(
            str(items[0]),
            items[1],
            self._span(meta),
            self._token_span(items[0]),
        )

    def rule_assignment(self, items: list[object]) -> ast_nodes.NextAssignment:
        target = items[0]
        if isinstance(target, _ParsedRuleTarget):
            return ast_nodes.NextAssignment(target.name, items[1], target.name_origin)
        return ast_nodes.NextAssignment(target, items[1])

    @v_args(meta=True)
    def output_drive(
        self, meta: object, items: list[object]
    ) -> ast_nodes.OutputDrive:
        return ast_nodes.OutputDrive(
            str(items[0]),
            items[1],
            self._token_span(items[0]) or self._name_span_from_meta(
                meta, str(items[0])
            ),
        )

    @staticmethod
    def _action_block(value: object) -> tuple[object, ...]:
        if not _tagged(value, "action_block"):
            raise ParseError("invalid atomic action block")
        return value[1]

    def action_block(self, items: list[object]) -> tuple[object, ...]:
        return (
            "action_block",
            tuple(
                item for item in items
                if isinstance(
                    item,
                    (
                        ast_nodes.NextAssignment,
                        ast_nodes.OutputDrive,
                        ast_nodes.ResourceAction,
                        ast_nodes.ConditionalAction,
                    ),
                )
            ),
        )

    def conditional_else_block(self, items: list[object]) -> tuple[object, ...]:
        return ("conditional_else", self._action_block(items[0]))

    def conditional_else_when(self, items: list[object]) -> tuple[object, ...]:
        return ("conditional_else", (items[0],))

    @v_args(meta=True)
    def conditional_action(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ConditionalAction:
        when_false = next(
            (item[1] for item in items[2:] if _tagged(item, "conditional_else")),
            None,
        )
        return ast_nodes.ConditionalAction(
            items[0],
            self._action_block(items[1]),
            when_false,
            self._span(meta),
        )

    @v_args(meta=True)
    def resource_action(self, meta: object, items: list[object]) -> ast_nodes.ResourceAction:
        return ast_nodes.ResourceAction(
            str(items[0]), str(items[1]),
            tuple(item for item in items[2:] if item is not None),
            self._span(meta),
        )

    @v_args(meta=True)
    def qualified_resource_action(
        self, meta: object, items: list[object]
    ) -> ast_nodes.ResourceAction:
        resource, action = str(items[0]).split(".", 1)
        return ast_nodes.ResourceAction(
            resource,
            action,
            tuple(item for item in items[1:] if item is not None),
            self._span(meta),
        )

    @v_args(meta=True)
    def rule_decl(self, meta: object, items: list[object]) -> ast_nodes.RuleDecl:
        origin = self._span(meta)
        domain = str(items[1]) if items[1] is not None else None
        offset = 1
        guard, actions = self._root_action_chain(
            items[1 + offset], items[2 + offset], items[3 + offset:], origin
        )
        if not actions:
            raise ParseError("atomic rule cannot be empty")
        return ast_nodes.RuleDecl(
            str(items[0]),
            guard,
            actions,
            origin,
            domain,
        )

    def concise_rule_decl(self, items: list[object]) -> ast_nodes.RuleDecl:
        """Normalize ``name: when guard { actions }`` to the rule AST."""

        return ast_nodes.RuleDecl(
            str(items[0]),
            items[1],
            self._action_block(items[2]),
        )

    @v_args(meta=True)
    def anonymous_rule_decl(self, meta: object, items: list[object]) -> ast_nodes.AnonymousRuleDecl:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[0], items[1], items[2:], origin
        )
        if not actions:
            raise ParseError("atomic rule cannot be empty")
        return ast_nodes.AnonymousRuleDecl(
            guard,
            actions,
            origin,
        )

    def rule_priority(self, items: list[object]) -> ast_nodes.RulePriority | ast_nodes.RulePriorityChain:
        names = tuple(str(item) for item in items)
        if len(names) == 2:
            return ast_nodes.RulePriority(*names)
        return ast_nodes.RulePriorityChain(names)

    @v_args(meta=True)
    def labeled_priority_arm(self, meta: object, items: list[object]) -> ast_nodes.PriorityRuleArm:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[1], items[2], items[3:], origin
        )
        return ast_nodes.PriorityRuleArm(
            str(items[0]),
            guard,
            actions,
            origin,
        )

    @v_args(meta=True)
    def anonymous_priority_arm(self, meta: object, items: list[object]) -> ast_nodes.PriorityRuleArm:
        origin = self._span(meta)
        guard, actions = self._root_action_chain(
            items[0], items[1], items[2:], origin
        )
        return ast_nodes.PriorityRuleArm(
            None,
            guard,
            actions,
            origin,
        )

    def nested_priority_arm(self, items: list[object]) -> object:
        return items[0]

    @v_args(meta=True)
    def priority_block(self, meta: object, items: list[object]) -> ast_nodes.PriorityBlockDecl:
        return ast_nodes.PriorityBlockDecl(tuple(items), self._span(meta))

    @v_args(meta=True)
    def fsm_transition(
        self, meta: object, items: list[object]
    ) -> ast_nodes.FsmTransitionDecl:
        block = next(
            item for item in items if _tagged(item, "action_block")
        )
        guard = next(
            (
                item for item in items
                if not isinstance(item, str) and item is not block
            ),
            None,
        )
        target = next(str(item) for item in items if isinstance(item, str))
        actions = self._action_block(block)
        return ast_nodes.FsmTransitionDecl(target, actions, guard, self._span(meta))

    def fsm_hold(self, _items: list[object]) -> tuple[str]:
        return ("fsm_hold",)

    def fsm_single_transition(
        self, items: list[object]
    ) -> tuple[str, tuple[ast_nodes.FsmTransitionDecl, ...]]:
        return ("fsm_transitions", (items[0],))

    def fsm_priority(
        self, items: list[object]
    ) -> tuple[str, tuple[ast_nodes.FsmTransitionDecl, ...]]:
        return ("fsm_priority", tuple(items))

    @v_args(meta=True)
    def fsm_state(self, meta: object, items: list[object]) -> ast_nodes.FsmStateDecl:
        member = str(items[0])
        body = items[1]
        if isinstance(body, tuple) and body[:1] == ("fsm_hold",):
            return ast_nodes.FsmStateDecl(member, hold=True, origin=self._span(meta))
        return ast_nodes.FsmStateDecl(
            member,
            body[1],
            priority=_tagged(body, "fsm_priority"),
            origin=self._span(meta),
        )

    @v_args(meta=True)
    def typed_fsm_decl(self, meta: object, items: list[object]) -> ast_nodes.FsmDecl:
        domain = str(items[2]) if items[2] is not None else None
        initial = str(items[3])
        return ast_nodes.FsmDecl(
            str(items[0]),
            items[1],
            initial,
            tuple(item for item in items[4:] if isinstance(item, ast_nodes.FsmStateDecl)),
            self._span(meta),
            domain,
        )

    @v_args(meta=True)
    def inferred_fsm_decl(self, meta: object, items: list[object]) -> ast_nodes.FsmDecl:
        domain = str(items[1]) if items[1] is not None else None
        enum_name, initial = str(items[2]), str(items[3])
        return ast_nodes.FsmDecl(
            str(items[0]),
            ast_nodes.TypeName(enum_name),
            initial,
            tuple(item for item in items[4:] if isinstance(item, ast_nodes.FsmStateDecl)),
            self._span(meta),
            domain,
        )

    @v_args(meta=True)
    def fifo_decl(self, meta: object, items: list[object]) -> ast_nodes.FifoDecl:
        domain = str(items[3]) if items[3] is not None else None
        return ast_nodes.FifoDecl(
            str(items[0]), items[1], items[2], self._span(meta), domain
        )

    @v_args(meta=True)
    def memory_decl(self, meta: object, items: list[object]) -> ast_nodes.MemoryDecl:
        reset_policy = next(
            (item for item in items if _tagged(item, "memory_reset_policy")),
            None,
        )
        kind = next(item for item in items if _tagged(item, "memory_kind"))
        ports = tuple(item for item in items if isinstance(item, ast_nodes.MemoryPortDecl))
        priority = next(
            (item for item in items if _tagged(item, "memory_write_priority")),
            None,
        )
        initializer = next(
            (item for item in items if _tagged(item, "memory_initializer")),
            None,
        )
        structural = [
            item for item in items[4:]
            if item is not None
            and not isinstance(item, ast_nodes.MemoryPortDecl)
            and not _tagged(item, "memory_reset_policy")
            and not _tagged(item, "memory_write_priority")
            and not _tagged(item, "memory_initializer")
        ]
        domain = str(structural[0]) if len(structural) == 3 else None
        latency, collision = structural[-2:]
        collision_name = str(collision)
        collision_aliases = {
            "old": ast_nodes.MemoryCollision.READ_FIRST,
            "new": ast_nodes.MemoryCollision.WRITE_FIRST,
            "no_change": ast_nodes.MemoryCollision.NO_CHANGE,
        }
        collision_value = collision_aliases.get(collision_name)
        if collision_value is None:
            collision_value = ast_nodes.MemoryCollision(collision_name)
        return ast_nodes.MemoryDecl(
            name=str(items[0]),
            element_type=items[2],
            depth=items[3],
            read_latency=self._parse_number(latency),
            collision=collision_value,
            origin=self._span(meta),
            contents_reset=(
                reset_policy[1]
                if reset_policy is not None else ast_nodes.MemoryResetPolicy.CLEAR
            ),
            read_data_reset=(
                reset_policy[2]
                if reset_policy is not None else ast_nodes.MemoryResetPolicy.CLEAR
            ),
            domain=domain,
            ports=ports,
            async_memory=kind[1] == "async",
            write_priority=priority[1] if priority is not None else (),
            initializer=initializer[1] if initializer is not None else None,
        )

    def synchronous_memory_kind(self, _items: list[object]) -> tuple[str, str]:
        return ("memory_kind", "synchronous")

    def asynchronous_memory_kind(self, _items: list[object]) -> tuple[str, str]:
        return ("memory_kind", "async")

    @v_args(meta=True)
    def memory_port_decl(
        self, meta: object, items: list[object]
    ) -> ast_nodes.MemoryPortDecl:
        spelling = str(items[0])
        kind = {
            "read_port": ast_nodes.MemoryPortKind.READ,
            "write_port": ast_nodes.MemoryPortKind.WRITE,
            "read_write_port": ast_nodes.MemoryPortKind.READ_WRITE,
        }[spelling]
        return ast_nodes.MemoryPortDecl(
            str(items[1]), kind,
            str(items[2]) if len(items) > 2 and items[2] is not None else None,
            self._span(meta),
        )

    def memory_write_priority(self, items: list[object]) -> tuple[object, ...]:
        return ("memory_write_priority", tuple(str(item) for item in items))

    def memory_initializer(self, items: list[object]) -> tuple[object, ...]:
        return ("memory_initializer", items[0])

    def memory_reset_policy(self, items: list[object]) -> tuple[object, ...]:
        return (
            "memory_reset_policy",
            ast_nodes.MemoryResetPolicy(str(items[0])),
            ast_nodes.MemoryResetPolicy(str(items[1])),
        )

    @v_args(meta=True)
    def rom_decl(self, meta: object, items: list[object]) -> ast_nodes.RomDecl:
        # Lark preserves the absent optional ``@ clock`` as ``None``.  Filter
        # it before deciding whether the declaration carries a domain; turning
        # it into the string ``"None"`` would make every legacy ROM appear to
        # reference an unknown physical clock.
        body = [item for item in items[3:] if item is not None]
        domain = str(body[0]) if len(body) == 3 else None
        latency, initializer = body[-2:]
        return ast_nodes.RomDecl(
            str(items[0]),
            items[1],
            items[2],
            int(str(latency)),
            initializer,
            self._span(meta),
            domain,
        )

    def port_name_list(self, items: list[object]) -> tuple[object, ...]:
        return (
            "port_names",
            tuple(str(item) for item in items),
            tuple(self._token_span(item) for item in items),
        )


__all__ = ["StateRules"]
