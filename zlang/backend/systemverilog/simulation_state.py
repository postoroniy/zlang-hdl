"""Verilator VPI companion for simulation-only architectural state access."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from zlang.backend.identifiers import (
    rtl_identifier,
    rtl_memory_cells_identifier,
    rtl_memory_read_data_identifier,
    rtl_register_state_identifier,
)
from zlang.backend.manifest import BackendArtifact
from zlang.backend.naming import RTL_NAMING_SCHEMA, module_rtl_names, rtl_hierarchy_instance_path
from zlang.common import stable_digest, stable_json
from zlang.ir.module import Module
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.simulation_state import (
    SimulationStateCatalog,
    SimulationStateError,
    SimulationStateKind,
    build_simulation_state_catalog,
)


SYSTEMVERILOG_SIMULATION_STATE_SCHEMA = "zlang-systemverilog-simulation-state-v2"


@dataclass(frozen=True)
class SystemVerilogStateLocator:
    binding_id: str
    vpi_path: str
    object_kind: SimulationStateKind
    packed_width: int
    element_width: int | None
    length: int | None

    def to_data(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "vpi_path": self.vpi_path,
            "object_kind": self.object_kind.value,
            "packed_width": self.packed_width,
            "element_width": self.element_width,
            "length": self.length,
            "element_zero_is_msb": True,
            "readable": True,
            "writable": True,
            "raw_word_bits": 32,
            "raw_word_order": "least_significant_word_first",
        }

    @classmethod
    def from_data(cls, value: object) -> "SystemVerilogStateLocator":
        if not isinstance(value, dict):
            raise SimulationStateError("SystemVerilog state locator must be an object")
        expected = {
            "binding_id", "vpi_path", "object_kind", "packed_width",
            "element_width", "length", "element_zero_is_msb", "readable",
            "writable", "raw_word_bits", "raw_word_order",
        }
        if set(value) != expected:
            raise SimulationStateError(
                "SystemVerilog state locator fields do not match the v1 schema"
            )
        if value["element_zero_is_msb"] is not True:
            raise SimulationStateError("simulation vector element zero must occupy the MSB")
        if value["readable"] is not True or value["writable"] is not True:
            raise SimulationStateError("simulation state locator must be read/write")
        if (
            value["raw_word_bits"] != 32
            or value["raw_word_order"] != "least_significant_word_first"
        ):
            raise SimulationStateError(
                "simulation state raw-word encoding is malformed"
            )
        for name in ("binding_id", "vpi_path", "object_kind"):
            if not isinstance(value[name], str) or not value[name]:
                raise SimulationStateError(
                    f"SystemVerilog state locator {name} must be non-empty"
                )
        for name in ("packed_width", "element_width", "length"):
            item = value[name]
            if item is None and name != "packed_width":
                continue
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise SimulationStateError(
                    f"SystemVerilog state locator {name} must be positive"
                )
        try:
            kind = SimulationStateKind(value["object_kind"])
        except ValueError as error:
            raise SimulationStateError(str(error)) from error
        return cls(
            value["binding_id"],
            value["vpi_path"],
            kind,
            value["packed_width"],
            value["element_width"],
            value["length"],
        )


@dataclass(frozen=True)
class SystemVerilogSimulationStateBundle:
    """Immutable access recipe bound to one unmodified production artifact."""

    catalog: SimulationStateCatalog
    artifact_hash: str
    artifact_build_identity: str
    rtl_module: str
    locators: tuple[SystemVerilogStateLocator, ...]
    bundle_identity: str
    schema: str = SYSTEMVERILOG_SIMULATION_STATE_SCHEMA

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "catalog_identity": self.catalog.catalog_identity,
            "backend": "direct_systemverilog",
            "artifact_hash": self.artifact_hash,
            "artifact_build_identity": self.artifact_build_identity,
            "rtl_module": self.rtl_module,
            "locators": [item.to_data() for item in self.locators],
            "required_verilator_flags": ["--vpi", "--public-flat-rw"],
        }

    def validate(self) -> None:
        if self.schema != SYSTEMVERILOG_SIMULATION_STATE_SCHEMA:
            raise SimulationStateError(
                "unsupported SystemVerilog simulation state bundle schema"
            )
        self.catalog.validate()
        if not self.artifact_hash or not self.artifact_build_identity or not self.rtl_module:
            raise SimulationStateError(
                "SystemVerilog simulation state artifact identities must not be empty"
            )
        by_id = {item.binding_id: item for item in self.catalog.bindings}
        if len(by_id) != len(self.catalog.bindings):
            raise SimulationStateError("simulation state catalog contains duplicate IDs")
        locator_ids = [item.binding_id for item in self.locators]
        if len(locator_ids) != len(set(locator_ids)) or set(locator_ids) != set(by_id):
            raise SimulationStateError(
                "SystemVerilog simulation state locators do not exactly cover the catalog"
            )
        locator_paths = [item.vpi_path for item in self.locators]
        if len(locator_paths) != len(set(locator_paths)):
            raise SimulationStateError(
                "SystemVerilog simulation state locators contain a duplicate VPI path"
            )
        for locator in self.locators:
            binding = by_id[locator.binding_id]
            if (
                locator.object_kind is not binding.object_kind
                or locator.packed_width != binding.packed_width
                or locator.element_width != binding.element_width
                or locator.length != binding.length
            ):
                raise SimulationStateError(
                    f"SystemVerilog locator '{locator.binding_id}' shape is stale"
                )
            if not locator.vpi_path.startswith(f"TOP.{self.rtl_module}."):
                raise SimulationStateError(
                    f"SystemVerilog locator '{locator.binding_id}' is outside the RTL root"
                )
        if self.bundle_identity != stable_digest(self._identity_payload()):
            raise SimulationStateError(
                "SystemVerilog simulation state bundle identity is stale"
            )

    def to_data(self) -> dict[str, object]:
        self.validate()
        return {
            "schema": self.schema,
            "catalog": self.catalog.to_data(),
            "backend": "direct_systemverilog",
            "artifact_hash": self.artifact_hash,
            "artifact_build_identity": self.artifact_build_identity,
            "rtl_module": self.rtl_module,
            "locators": [item.to_data() for item in self.locators],
            "required_verilator_flags": ["--vpi", "--public-flat-rw"],
            "bundle_identity": self.bundle_identity,
        }

    def validate_artifact(self, artifact: BackendArtifact) -> None:
        self.validate()
        actual_hash = hashlib.sha256(artifact.text.encode()).hexdigest()
        if (
            artifact.backend != "direct_systemverilog"
            or artifact.module != self.catalog.module
            or artifact.selected_ir_identity != self.catalog.selected_ir_identity
            or actual_hash != artifact.artifact_hash
            or artifact.artifact_hash != self.artifact_hash
            or artifact.build_identity != self.artifact_build_identity
            or artifact.naming_schema != RTL_NAMING_SCHEMA
        ):
            raise SimulationStateError(
                "simulation state bundle does not match the production artifact"
            )

    def validate_rtl_file(self, path: Path) -> None:
        """Reject a missing or modified RTL file before publishing locators."""

        self.validate()
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise SimulationStateError(
                f"cannot validate simulation-state RTL artifact '{path}': {error}"
            ) from error
        if hashlib.sha256(payload).hexdigest() != self.artifact_hash:
            raise SimulationStateError(
                "simulation state bundle does not match the published RTL file"
            )

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, value: object) -> "SystemVerilogSimulationStateBundle":
        if not isinstance(value, dict):
            raise SimulationStateError(
                "SystemVerilog simulation state bundle must be an object"
            )
        expected = {
            "schema", "catalog", "backend", "artifact_hash",
            "artifact_build_identity", "rtl_module", "locators",
            "required_verilator_flags", "bundle_identity",
        }
        if set(value) != expected:
            raise SimulationStateError(
                "SystemVerilog simulation state bundle fields do not match v2"
            )
        if value["backend"] != "direct_systemverilog":
            raise SimulationStateError(
                "simulation state bundle backend must be direct_systemverilog"
            )
        if value["required_verilator_flags"] != ["--vpi", "--public-flat-rw"]:
            raise SimulationStateError("simulation state bundle flags are malformed")
        raw_locators = value["locators"]
        if not isinstance(raw_locators, list):
            raise SimulationStateError("simulation state locators must be an array")
        for name in (
            "schema", "artifact_hash", "artifact_build_identity", "rtl_module",
            "bundle_identity",
        ):
            if not isinstance(value[name], str) or not value[name]:
                raise SimulationStateError(
                    f"SystemVerilog simulation bundle {name} must be non-empty"
                )
        result = cls(
            SimulationStateCatalog.from_data(value["catalog"]),
            value["artifact_hash"],
            value["artifact_build_identity"],
            value["rtl_module"],
            tuple(SystemVerilogStateLocator.from_data(item) for item in raw_locators),
            value["bundle_identity"],
            value["schema"],
        )
        result.validate()
        return result

    @classmethod
    def from_json(cls, text: str) -> "SystemVerilogSimulationStateBundle":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise SimulationStateError(
                f"invalid SystemVerilog simulation state JSON: {error}"
            ) from error
        return cls.from_data(value)

    def cpp_header(self) -> str:
        self.validate()
        records = "\n".join(
            "    {\""
            + item.binding_id
            + "\", \""
            + item.vpi_path
            + "\", "
            + str(item.packed_width)
            + ", "
            + str(item.element_width or 0)
            + ", "
            + str(item.length or 0)
            + ", "
            + ("true" if item.object_kind is SimulationStateKind.MEMORY else "false")
            + "},"
            for item in self.locators
        )
        guard = "ZLANG_SIM_STATE_" + self.bundle_identity.upper()
        return f"""// Generated simulation-only VPI access; not synthesized RTL.
#ifndef {guard}
#define {guard}

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include \"vpi_user.h\"

namespace zlang_simulation {{

using RawWords = std::vector<uint32_t>;

struct Binding {{
  const char* semantic_id;
  const char* vpi_path;
  unsigned packed_width;
  unsigned element_width;
  unsigned length;
  bool memory;
}};

inline constexpr Binding bindings[] = {{
{records}
}};

class StateAccess {{
 public:
  StateAccess() {{
    // Resolve and validate the complete allow-list before the first deposit,
    // so stale RTL cannot produce a partially applied preload.
    for (const auto& item : bindings) {{
      vpiHandle handle = resolve(item);
      if (item.memory && item.length != 0) {{
        vpiHandle element = vpi_handle_by_index(handle, 0);
        if (element == nullptr ||
            vpi_get(vpiSize, element) != static_cast<int>(item.element_width)) {{
          throw std::runtime_error(std::string("stale VPI state element: ") + item.vpi_path);
        }}
      }}
    }}
  }}

  const Binding& binding(const std::string& semantic_id) const {{
    for (const auto& item : bindings) {{
      if (semantic_id == item.semantic_id) return item;
    }}
    throw std::runtime_error("unknown ZLang simulation state binding: " + semantic_id);
  }}

  uint64_t read_u64(const std::string& semantic_id) const {{
    const Binding& item = binding(semantic_id);
    if (item.length != 0 || item.packed_width > 64) {{
      throw std::runtime_error("state binding is not a <=64-bit scalar: " + semantic_id);
    }}
    return words_to_u64(
        read_packed_words(resolve(item), 0, item.packed_width),
        item.packed_width);
  }}

  void write_u64(const std::string& semantic_id, uint64_t value) const {{
    const Binding& item = binding(semantic_id);
    if (item.length != 0 || item.packed_width > 64) {{
      throw std::runtime_error("state binding is not a <=64-bit scalar: " + semantic_id);
    }}
    if (item.packed_width < 64U && (value >> item.packed_width) != 0U) {{
      throw std::runtime_error("state value exceeds the exact packed width: " + semantic_id);
    }}
    RawWords words((item.packed_width + 31U) / 32U, 0);
    words[0] = static_cast<uint32_t>(value);
    if (item.packed_width > 32) words[1] = static_cast<uint32_t>(value >> 32U);
    write_packed_words(resolve(item), 0, item.packed_width, words);
  }}

  RawWords read_words(const std::string& semantic_id) const {{
    const Binding& item = binding(semantic_id);
    if (item.length != 0) {{
      throw std::runtime_error("indexed state requires read_element_words: " + semantic_id);
    }}
    return read_packed_words(resolve(item), 0, item.packed_width);
  }}

  void write_words(
      const std::string& semantic_id, const RawWords& words) const {{
    const Binding& item = binding(semantic_id);
    if (item.length != 0) {{
      throw std::runtime_error("indexed state requires write_element_words: " + semantic_id);
    }}
    write_packed_words(resolve(item), 0, item.packed_width, words);
  }}

  uint64_t read_element_u64(const std::string& semantic_id, unsigned index) const {{
    const Binding& item = binding(semantic_id);
    check_index(item, index);
    if (item.element_width > 64) {{
      throw std::runtime_error("state element is wider than 64 bits: " + semantic_id);
    }}
    return words_to_u64(read_element_words(semantic_id, index), item.element_width);
  }}

  void write_element_u64(
      const std::string& semantic_id, unsigned index, uint64_t value) const {{
    const Binding& item = binding(semantic_id);
    check_index(item, index);
    if (item.element_width > 64) {{
      throw std::runtime_error("state element is wider than 64 bits: " + semantic_id);
    }}
    if (item.element_width < 64U && (value >> item.element_width) != 0U) {{
      throw std::runtime_error("state value exceeds the exact element width: " + semantic_id);
    }}
    RawWords words((item.element_width + 31U) / 32U, 0);
    words[0] = static_cast<uint32_t>(value);
    if (item.element_width > 32) words[1] = static_cast<uint32_t>(value >> 32U);
    write_element_words(semantic_id, index, words);
  }}

  RawWords read_element_words(
      const std::string& semantic_id, unsigned index) const {{
    const Binding& item = binding(semantic_id);
    check_index(item, index);
    if (item.memory) {{
      return read_packed_words(
          resolve_element(item, index), 0, item.element_width);
    }}
    const unsigned lsb = item.packed_width - (index + 1U) * item.element_width;
    return read_packed_words(resolve(item), lsb, item.element_width);
  }}

  void write_element_words(
      const std::string& semantic_id, unsigned index,
      const RawWords& words) const {{
    const Binding& item = binding(semantic_id);
    check_index(item, index);
    if (item.memory) {{
      write_packed_words(
          resolve_element(item, index), 0, item.element_width, words);
      return;
    }}
    const unsigned lsb = item.packed_width - (index + 1U) * item.element_width;
    write_packed_words(resolve(item), lsb, item.element_width, words);
  }}

 private:
  static vpiHandle resolve(const Binding& item) {{
    vpiHandle handle = vpi_handle_by_name(
        const_cast<PLI_BYTE8*>(reinterpret_cast<const PLI_BYTE8*>(item.vpi_path)),
        nullptr);
    if (handle == nullptr) {{
      throw std::runtime_error(std::string("missing VPI state object: ") + item.vpi_path);
    }}
    const int expected = item.memory ? static_cast<int>(item.length)
                                     : static_cast<int>(item.packed_width);
    if (vpi_get(vpiSize, handle) != expected) {{
      throw std::runtime_error(std::string("stale VPI state shape: ") + item.vpi_path);
    }}
    return handle;
  }}

  static vpiHandle resolve_element(const Binding& item, unsigned index) {{
    vpiHandle element = vpi_handle_by_index(resolve(item), static_cast<PLI_INT32>(index));
    if (element == nullptr || vpi_get(vpiSize, element) != static_cast<int>(item.element_width)) {{
      throw std::runtime_error(std::string("stale VPI state element: ") + item.vpi_path);
    }}
    return element;
  }}

  static void check_index(const Binding& item, unsigned index) {{
    if (item.length == 0 || index >= item.length) {{
      throw std::runtime_error("state element index is out of range");
    }}
  }}

  static uint64_t words_to_u64(const RawWords& words, unsigned width) {{
    if (width > 64 || words.empty()) {{
      throw std::runtime_error("raw state value is not a <=64-bit scalar");
    }}
    uint64_t result = 0;
    for (unsigned bit = 0; bit < width; ++bit) {{
      const uint64_t value = (words[bit / 32U] >> (bit % 32U)) & 1U;
      result |= value << bit;
    }}
    return result;
  }}

  static RawWords read_packed_words(
      vpiHandle packed, unsigned lsb, unsigned width) {{
    const unsigned packed_width = static_cast<unsigned>(vpi_get(vpiSize, packed));
    if (width == 0 || lsb > packed_width || width > packed_width - lsb) {{
      throw std::runtime_error("packed VPI access is outside the object width");
    }}
    RawWords result((width + 31U) / 32U, 0);
    if (lsb == 0 && width == packed_width) {{
      s_vpi_value value{{}};
      value.format = vpiVectorVal;
      vpi_get_value(packed, &value);
      if (value.value.vector == nullptr) {{
        throw std::runtime_error("packed VPI object has no vector value");
      }}
      for (unsigned word = 0; word < result.size(); ++word) {{
        if (static_cast<uint32_t>(value.value.vector[word].bval) != 0) {{
          throw std::runtime_error("packed VPI state contains X or Z bits");
        }}
        result[word] = static_cast<uint32_t>(value.value.vector[word].aval);
      }}
      const unsigned high_bits = width % 32U;
      if (high_bits != 0) result.back() &= (uint32_t{{1}} << high_bits) - 1U;
      return result;
    }}
    for (unsigned bit = 0; bit < width; ++bit) {{
      vpiHandle selected = vpi_handle_by_index(
          packed, static_cast<PLI_INT32>(lsb + bit));
      if (selected == nullptr || vpi_get(vpiSize, selected) != 1) {{
        throw std::runtime_error("missing packed VPI bit-select");
      }}
      s_vpi_value value{{}};
      value.format = vpiVectorVal;
      vpi_get_value(selected, &value);
      if (value.value.vector == nullptr || value.value.vector[0].bval != 0) {{
        throw std::runtime_error("packed VPI state contains X or Z bits");
      }}
      if ((static_cast<uint32_t>(value.value.vector[0].aval) & 1U) != 0) {{
        result[bit / 32U] |= uint32_t{{1}} << (bit % 32U);
      }}
    }}
    return result;
  }}

  static void write_packed_words(
      vpiHandle packed, unsigned lsb, unsigned width,
      const RawWords& words) {{
    const unsigned expected_words = (width + 31U) / 32U;
    if (words.size() != expected_words) {{
      throw std::runtime_error("raw state value has the wrong word count");
    }}
    const unsigned high_bits = width % 32U;
    if (high_bits != 0 && (words.back() >> high_bits) != 0) {{
      throw std::runtime_error("raw state value exceeds the exact packed width");
    }}
    const unsigned packed_width = static_cast<unsigned>(vpi_get(vpiSize, packed));
    if (width == 0 || lsb > packed_width || width > packed_width - lsb) {{
      throw std::runtime_error("packed VPI access is outside the object width");
    }}
    if (lsb == 0 && width == packed_width) {{
      const unsigned vector_words = (packed_width + 31U) / 32U;
      std::vector<s_vpi_vecval> vector(vector_words, s_vpi_vecval{{0, 0}});
      for (unsigned word = 0; word < vector_words; ++word) {{
        vector[word].aval = words[word];
      }}
      s_vpi_value put{{}};
      put.format = vpiVectorVal;
      put.value.vector = vector.data();
      vpi_put_value(packed, &put, nullptr, vpiNoDelay);
      return;
    }}
    std::vector<vpiHandle> selected(width, nullptr);
    for (unsigned bit = 0; bit < width; ++bit) {{
      selected[bit] = vpi_handle_by_index(
          packed, static_cast<PLI_INT32>(lsb + bit));
      if (selected[bit] == nullptr || vpi_get(vpiSize, selected[bit]) != 1) {{
        throw std::runtime_error("missing packed VPI bit-select");
      }}
    }}
    for (unsigned bit = 0; bit < width; ++bit) {{
      s_vpi_value put{{}};
      put.format = vpiIntVal;
      put.value.integer = static_cast<PLI_INT32>(
          (words[bit / 32U] >> (bit % 32U)) & 1U);
      vpi_put_value(selected[bit], &put, nullptr, vpiNoDelay);
    }}
  }}
}};

}}  // namespace zlang_simulation

#endif
"""

    def publish(self, directory: Path) -> tuple[Path, Path]:
        self.validate()
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.json"
        header = directory / "simulation_state.hpp"
        manifest.write_text(self.to_json(), encoding="utf-8")
        header.write_text(self.cpp_header(), encoding="utf-8")
        return manifest, header


def build_systemverilog_simulation_state_bundle(
    module: Module,
    artifact: BackendArtifact,
) -> SystemVerilogSimulationStateBundle:
    """Bind the typed catalog to exact public VPI hierarchy paths."""

    if artifact.backend != "direct_systemverilog":
        raise SimulationStateError(
            "simulation state VPI bundle requires a direct-SystemVerilog artifact"
        )
    if artifact.naming_schema != RTL_NAMING_SCHEMA:
        raise SimulationStateError(
            "simulation state artifact naming schema is stale or unavailable"
        )
    if hashlib.sha256(artifact.text.encode()).hexdigest() != artifact.artifact_hash:
        raise SimulationStateError(
            "simulation state artifact text does not match its published hash"
        )
    if artifact.module != module.name:
        raise SimulationStateError(
            "simulation state artifact module does not match the typed root"
        )
    if artifact.implementation is not None:
        raise SimulationStateError(
            "simulation state VPI bindings are unavailable for target-mapped RTL"
        )
    catalog = build_simulation_state_catalog(
        module, selected_ir_identity=artifact.selected_ir_identity
    )
    from zlang.backend.systemverilog.emitter import (
        _physicalize_generic_callables,
        physical_state_root_path,
    )

    rtl_module = rtl_identifier(module.name)
    state_root = physical_state_root_path(module)
    # Generic helpers are renamed before RTL scope allocation.  Resolve VPI
    # through that same emission-only view, while retaining the original typed
    # semantic catalog above.  Otherwise a user instance colliding with a
    # physical helper could receive a different suffix here than in the RTL.
    typed_hierarchy = build_hierarchy_index(_physicalize_generic_callables(module))
    names_by_path = {
        entry.physical_path: module_rtl_names(entry.module)
        for entry in typed_hierarchy.entries
    }
    locators: list[SystemVerilogStateLocator] = []
    for binding in catalog.bindings:
        hierarchy = ".".join(rtl_hierarchy_instance_path(
            typed_hierarchy, binding.physical_instance_path, plans=names_by_path,
        ))
        if binding.object_kind is SimulationStateKind.REGISTER:
            token = rtl_register_state_identifier(binding.object_name)
        elif binding.object_kind is SimulationStateKind.MEMORY:
            token = rtl_memory_cells_identifier(binding.object_name)
        else:
            token = rtl_memory_read_data_identifier(binding.object_name)
        relative = f"{hierarchy}.{token}" if hierarchy else token
        locators.append(SystemVerilogStateLocator(
            binding.binding_id,
            ".".join((*state_root, relative)),
            binding.object_kind,
            binding.packed_width,
            binding.element_width,
            binding.length,
        ))
    if not locators:
        raise SimulationStateError(
            "selected design has no simulation-accessible register or memory state"
        )
    provisional = SystemVerilogSimulationStateBundle(
        catalog,
        artifact.artifact_hash,
        artifact.build_identity,
        rtl_module,
        tuple(locators),
        "pending",
    )
    result = SystemVerilogSimulationStateBundle(
        provisional.catalog,
        provisional.artifact_hash,
        provisional.artifact_build_identity,
        provisional.rtl_module,
        provisional.locators,
        stable_digest(provisional._identity_payload()),
    )
    result.validate()
    return result


__all__ = [
    "SYSTEMVERILOG_SIMULATION_STATE_SCHEMA",
    "SystemVerilogSimulationStateBundle",
    "SystemVerilogStateLocator",
    "build_systemverilog_simulation_state_bundle",
]
