"""Bounded compiler-owned memoization for formal preparation artifacts.

The provider owns *recipes*, not backend semantics.  A recipe is deterministic
JSON data in one frozen formal namespace.  Immutable compiler objects can be
memoized for the lifetime of a compilation session; values with an explicit
lossless JSON codec may additionally be reused through a cache root.

BackendArtifact manifests intentionally do not contain implementation or
companion text.  Callers must therefore not invent a decoder for prepared
backend artifacts: those values remain session-local until a complete public
serialization exists.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
from threading import Event, RLock
from typing import Callable, Generic, Mapping, TypeVar

from zlang.backend.manifest import BackendArtifact, backend_binding_identity
from zlang.common import stable_digest, stable_json
from zlang.common.content_cache import load_json_object, publish_json_atomically
from zlang.ir.formal_planning import FormalBackendArtifactRef


FORMAL_ARTIFACT_RECIPE_SCHEMA = "zlang-formal-artifact-recipe-v1"
FORMAL_ARTIFACT_CACHE_SCHEMA = "zlang-formal-artifact-cache-v1"


def formal_backend_artifact_ref(
    artifact: BackendArtifact,
) -> FormalBackendArtifactRef:
    """Describe one backend artifact using the shared formal binding identity."""

    return FormalBackendArtifactRef(
        artifact.backend,
        artifact.artifact_hash,
        backend_binding_identity(artifact),
    )


class FormalArtifactNamespace(str, Enum):
    """Closed namespaces for the currently supported formal stack."""

    PREPARED = "prepared"
    M35 = "M35"
    M36 = "M36"
    M39 = "M39"


FORMAL_ARTIFACT_NAMESPACES = frozenset(item.value for item in FormalArtifactNamespace)


class FormalArtifactProviderError(ValueError):
    """A recipe, codec, or cache entry violates the provider contract."""


def _json_value(value: object, *, field: str) -> object:
    """Return a detached strict JSON value without ``default=str`` fallback."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FormalArtifactProviderError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, field=f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FormalArtifactProviderError(
                f"{field} objects require string keys"
            )
        return {
            key: _json_value(value[key], field=f"{field}.{key}")
            for key in sorted(value)
        }
    raise FormalArtifactProviderError(
        f"{field} contains unsupported value type {type(value).__name__}"
    )


def _namespace(value: FormalArtifactNamespace | str) -> str:
    try:
        namespace = FormalArtifactNamespace(value).value
    except (TypeError, ValueError) as error:
        allowed = ", ".join(item.value for item in FormalArtifactNamespace)
        raise FormalArtifactProviderError(
            f"formal artifact namespace must be one of: {allowed}"
        ) from error
    return namespace


@dataclass(frozen=True, init=False)
class FormalArtifactRecipe:
    """One immutable, content-addressed formal preparation request."""

    namespace: str
    kind: str
    _inputs_json: str
    identity: str

    def __init__(
        self,
        namespace: FormalArtifactNamespace | str,
        kind: str,
        inputs: Mapping[str, object],
    ) -> None:
        normalized_namespace = _namespace(namespace)
        if not isinstance(kind, str) or not kind:
            raise FormalArtifactProviderError(
                "formal artifact recipe kind must be a non-empty string"
            )
        normalized_inputs = _json_value(inputs, field="recipe inputs")
        assert isinstance(normalized_inputs, dict)
        inputs_json = stable_json(normalized_inputs)
        base = {
            "schema": FORMAL_ARTIFACT_RECIPE_SCHEMA,
            "namespace": normalized_namespace,
            "kind": kind,
            "inputs": normalized_inputs,
        }
        object.__setattr__(self, "namespace", normalized_namespace)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_inputs_json", inputs_json)
        object.__setattr__(
            self,
            "identity",
            "formal-recipe:" + stable_digest(base),
        )

    @property
    def inputs(self) -> dict[str, object]:
        """Return a detached copy so callers cannot mutate recipe identity."""

        value = json.loads(self._inputs_json)
        assert isinstance(value, dict)
        return value

    @property
    def digest(self) -> str:
        return self.identity.removeprefix("formal-recipe:")

    def to_data(self) -> dict[str, object]:
        return {
            "schema": FORMAL_ARTIFACT_RECIPE_SCHEMA,
            "namespace": self.namespace,
            "kind": self.kind,
            "inputs": self.inputs,
        }

    @classmethod
    def from_data(cls, value: object) -> "FormalArtifactRecipe":
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise FormalArtifactProviderError("cached recipe must be a JSON object")
        if set(value) != {"schema", "namespace", "kind", "inputs"}:
            raise FormalArtifactProviderError("cached recipe fields are invalid")
        if value.get("schema") != FORMAL_ARTIFACT_RECIPE_SCHEMA:
            raise FormalArtifactProviderError("cached recipe schema is unsupported")
        inputs = value.get("inputs")
        if not isinstance(inputs, Mapping):
            raise FormalArtifactProviderError("cached recipe inputs must be an object")
        return cls(str(value.get("namespace")), str(value.get("kind")), inputs)


_DECISIVE_STATUSES = frozenset(
    {"proven", "failed", "bounded_pass", "witnessed", "bounded_unreached"}
)
_NON_CACHEABLE_STATUSES = frozenset({"unknown", "skipped", "timeout", "timed_out"})


def decisive_formal_cacheable(value: object) -> bool:
    """Reject absent and explicitly inconclusive formal outcomes.

    Prepared artifacts and recipes have no ``status`` and are cacheable.  A
    result-shaped value is cacheable only when its status is decisive.  This
    keeps timeouts and environment-dependent skips retryable.
    """

    if value is None:
        return False
    status: object | None = None
    has_status = False
    if isinstance(value, Mapping) and "status" in value:
        status = value.get("status")
        has_status = True
    elif hasattr(value, "status"):
        status = getattr(value, "status")
        has_status = True
    if not has_status:
        return True
    token = getattr(status, "value", status)
    normalized = str(token).strip().lower()
    if normalized in _NON_CACHEABLE_STATUSES:
        return False
    return normalized in _DECISIVE_STATUSES


T = TypeVar("T")


@dataclass(frozen=True)
class FormalArtifactProviderStats:
    memory_hits: int = 0
    disk_hits: int = 0
    misses: int = 0
    publications: int = 0
    corruptions: int = 0
    evictions: int = 0


@dataclass
class _MemoryEntry(Generic[T]):
    value: T
    fingerprint: str | None


@dataclass
class _Pending(Generic[T]):
    ready: Event
    value: T | object
    error: BaseException | None = None


_MISSING = object()


class FormalArtifactProvider:
    """Thread-safe bounded recipe cache owned by a compilation session.

    ``max_entries`` bounds the in-memory LRU.  Disk reuse is opt-in per call:
    both ``encode`` and ``decode`` must be supplied, and the decoder must
    round-trip through the encoder exactly.  Compiler objects without a full
    lossless codec are intentionally memoized only in memory.
    """

    def __init__(
        self,
        cache_root: Path | str | None = None,
        *,
        max_entries: int = 256,
    ) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
            or max_entries > 4096
        ):
            raise FormalArtifactProviderError(
                "formal artifact provider max_entries must be between 1 and 4096"
            )
        self.cache_root = None if cache_root is None else Path(cache_root)
        self.max_entries = max_entries
        self._entries: OrderedDict[str, _MemoryEntry[object]] = OrderedDict()
        self._pending: dict[str, _Pending[object]] = {}
        self._lock = RLock()
        self._stats = FormalArtifactProviderStats()
        self._diagnostics: dict[str, str] = {}

    @property
    def stats(self) -> FormalArtifactProviderStats:
        with self._lock:
            return self._stats

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def diagnostic_for(self, recipe: FormalArtifactRecipe) -> str | None:
        with self._lock:
            return self._diagnostics.get(recipe.identity)

    def recipe(
        self,
        namespace: FormalArtifactNamespace | str,
        kind: str,
        inputs: Mapping[str, object],
    ) -> FormalArtifactRecipe:
        return FormalArtifactRecipe(namespace, kind, inputs)

    def entry_path(self, recipe: FormalArtifactRecipe) -> Path | None:
        if self.cache_root is None:
            return None
        return self.cache_root / recipe.namespace / f"{recipe.digest}.json"

    def clear_memory(self) -> None:
        """Forget session values without deleting explicit cache-root data."""

        with self._lock:
            self._entries.clear()

    def get_or_prepare(
        self,
        namespace: FormalArtifactNamespace | str,
        kind: str,
        inputs: Mapping[str, object],
        prepare: Callable[[], T],
        *,
        encode: Callable[[T], object] | None = None,
        decode: Callable[[object], T] | None = None,
        cacheable: Callable[[T], bool] = decisive_formal_cacheable,
        fingerprint: Callable[[T], object] | None = None,
    ) -> T:
        """Return one value for a deterministic recipe, preparing on a miss."""

        return self.memoize(
            FormalArtifactRecipe(namespace, kind, inputs),
            prepare,
            encode=encode,
            decode=decode,
            cacheable=cacheable,
            fingerprint=fingerprint,
        )

    def memoize(
        self,
        recipe: FormalArtifactRecipe,
        prepare: Callable[[], T],
        *,
        encode: Callable[[T], object] | None = None,
        decode: Callable[[object], T] | None = None,
        cacheable: Callable[[T], bool] = decisive_formal_cacheable,
        fingerprint: Callable[[T], object] | None = None,
    ) -> T:
        if not isinstance(recipe, FormalArtifactRecipe):
            raise TypeError("formal artifact provider requires a FormalArtifactRecipe")
        if not callable(prepare) or not callable(cacheable):
            raise TypeError("formal artifact preparation and cache policy must be callable")
        if (encode is None) != (decode is None):
            raise FormalArtifactProviderError(
                "disk-backed formal artifact reuse requires both encode and decode"
            )
        if fingerprint is not None and not callable(fingerprint):
            raise TypeError("formal artifact fingerprint must be callable")

        key = recipe.identity
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if self._entry_matches(entry, fingerprint, encode):
                    self._entries.move_to_end(key)
                    self._bump(memory_hits=1)
                    return entry.value  # type: ignore[return-value]
                del self._entries[key]
                self._diagnostics[key] = "in-memory fingerprint mismatch"
                self._bump(corruptions=1)
            pending = self._pending.get(key)
            if pending is None:
                pending = _Pending(Event(), _MISSING)
                self._pending[key] = pending
                leader = True
            else:
                leader = False

        if not leader:
            pending.ready.wait()
            if pending.error is not None:
                raise pending.error
            assert pending.value is not _MISSING
            return pending.value  # type: ignore[return-value]

        try:
            value = self._load_disk(recipe, encode, decode, cacheable)
            if value is _MISSING:
                with self._lock:
                    self._bump(misses=1)
                prepared = prepare()
                if cacheable(prepared):
                    value_fingerprint = self._fingerprint(
                        prepared,
                        fingerprint,
                        encode,
                    )
                    if (
                        self.cache_root is not None
                        and encode is not None
                        and decode is not None
                    ):
                        self._publish_disk(recipe, prepared, encode, decode)
                    self._remember(key, prepared, value_fingerprint)
                value = prepared
            else:
                prepared = value  # type: ignore[assignment]
                self._remember(
                    key,
                    prepared,
                    self._fingerprint(prepared, fingerprint, encode),
                )
            pending.value = value
            return value  # type: ignore[return-value]
        except BaseException as error:
            pending.error = error
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)
                pending.ready.set()

    def _entry_matches(
        self,
        entry: _MemoryEntry[object],
        fingerprint: Callable[[object], object] | None,
        encode: Callable[[object], object] | None,
    ) -> bool:
        if entry.fingerprint is None and fingerprint is None and encode is None:
            return True
        if entry.fingerprint is None or (fingerprint is None and encode is None):
            return False
        try:
            current = self._fingerprint(entry.value, fingerprint, encode)
        except (FormalArtifactProviderError, TypeError, ValueError):
            return False
        return current == entry.fingerprint

    def _fingerprint(
        self,
        value: T,
        fingerprint: Callable[[T], object] | None,
        encode: Callable[[T], object] | None,
    ) -> str | None:
        identity = fingerprint or encode
        if identity is None:
            return None
        payload = _json_value(identity(value), field="artifact fingerprint")
        return stable_digest(payload)

    def _remember(self, key: str, value: T, fingerprint: str | None) -> None:
        with self._lock:
            self._entries[key] = _MemoryEntry(value, fingerprint)
            self._entries.move_to_end(key)
            evicted = 0
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                evicted += 1
            if evicted:
                self._bump(evictions=evicted)

    def _load_disk(
        self,
        recipe: FormalArtifactRecipe,
        encode: Callable[[T], object] | None,
        decode: Callable[[object], T] | None,
        cacheable: Callable[[T], bool],
    ) -> T | object:
        path = self.entry_path(recipe)
        if path is None or encode is None or decode is None:
            return _MISSING
        envelope, diagnostic = load_json_object(path)
        if diagnostic is not None:
            self._reject(recipe, diagnostic)
            return _MISSING
        if envelope is None:
            return _MISSING
        try:
            expected_fields = {
                "schema",
                "namespace",
                "recipe",
                "recipe_identity",
                "value",
                "value_hash",
            }
            if set(envelope) != expected_fields:
                raise FormalArtifactProviderError("cache envelope fields are invalid")
            if envelope.get("schema") != FORMAL_ARTIFACT_CACHE_SCHEMA:
                raise FormalArtifactProviderError("cache envelope schema is unsupported")
            if envelope.get("namespace") != recipe.namespace:
                raise FormalArtifactProviderError("cache namespace does not match recipe")
            cached_recipe = FormalArtifactRecipe.from_data(envelope.get("recipe"))
            if cached_recipe.identity != recipe.identity:
                raise FormalArtifactProviderError("cached recipe identity does not match")
            if envelope.get("recipe_identity") != recipe.identity:
                raise FormalArtifactProviderError("cache recipe hash does not match")
            encoded = _json_value(envelope.get("value"), field="cached value")
            expected_hash = envelope.get("value_hash")
            if not isinstance(expected_hash, str) or expected_hash != stable_digest(encoded):
                raise FormalArtifactProviderError("cached value hash does not match")
            value = decode(encoded)
            round_trip = _json_value(encode(value), field="decoded value")
            if stable_json(round_trip) != stable_json(encoded):
                raise FormalArtifactProviderError(
                    "cached value codec does not round-trip exactly"
                )
            if not cacheable(value):
                raise FormalArtifactProviderError(
                    "cached formal outcome is not decisive"
                )
        except Exception as error:
            self._reject(recipe, str(error))
            return _MISSING
        with self._lock:
            self._diagnostics.pop(recipe.identity, None)
            self._bump(disk_hits=1)
        return value

    def _publish_disk(
        self,
        recipe: FormalArtifactRecipe,
        value: T,
        encode: Callable[[T], object],
        decode: Callable[[object], T],
    ) -> None:
        path = self.entry_path(recipe)
        assert path is not None
        encoded = _json_value(encode(value), field="encoded value")
        decoded = decode(encoded)
        round_trip = _json_value(encode(decoded), field="decoded value")
        if stable_json(round_trip) != stable_json(encoded):
            raise FormalArtifactProviderError(
                "formal artifact codec does not round-trip exactly"
            )
        publish_json_atomically(path, {
            "schema": FORMAL_ARTIFACT_CACHE_SCHEMA,
            "namespace": recipe.namespace,
            "recipe": recipe.to_data(),
            "recipe_identity": recipe.identity,
            "value": encoded,
            "value_hash": stable_digest(encoded),
        })
        with self._lock:
            self._diagnostics.pop(recipe.identity, None)
            self._bump(publications=1)

    def _reject(self, recipe: FormalArtifactRecipe, diagnostic: str) -> None:
        with self._lock:
            self._diagnostics[recipe.identity] = f"corrupt-ignored ({diagnostic})"
            self._bump(corruptions=1)

    def _bump(self, **updates: int) -> None:
        values = {
            name: getattr(self._stats, name) + updates.get(name, 0)
            for name in FormalArtifactProviderStats.__dataclass_fields__
        }
        self._stats = FormalArtifactProviderStats(**values)


__all__ = [
    "FORMAL_ARTIFACT_CACHE_SCHEMA",
    "FORMAL_ARTIFACT_NAMESPACES",
    "FORMAL_ARTIFACT_RECIPE_SCHEMA",
    "FormalArtifactNamespace",
    "FormalArtifactProvider",
    "FormalArtifactProviderError",
    "FormalArtifactProviderStats",
    "FormalArtifactRecipe",
    "decisive_formal_cacheable",
    "formal_backend_artifact_ref",
]
