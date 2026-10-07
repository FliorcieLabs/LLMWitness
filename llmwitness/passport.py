"""Local, fail-closed Passport delegation reference profile.

This module is deliberately a small Community reference implementation. It is
not an OAuth, VC, UCAN, or Open Agent Passport implementation and it does not
provide issuer trust, key custody, revocation infrastructure, or effect truth.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmwitness.authority import AuthorityEngine, AuthorityPolicy, AuthorityResult
from llmwitness.envelope import (
    AuthorityDecision,
    EvidenceReference,
    RiskTier,
    sha256_ref,
)
from llmwitness.utils import Ed25519KeyManager, canonical_json, generate_uuidv7

_RISK_ORDER = {RiskTier.R0: 0, RiskTier.R1: 1, RiskTier.R2: 2, RiskTier.R3: 3}
_LIMITATIONS = (
    "Local reference profile only; configured key trust and supplied status are "
    "not organizational identity, current provider authorization, or proof that "
    "an external effect occurred."
)


class PassportCapability(BaseModel):
    """The only capabilities a delegation can describe in this profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effects: tuple[str, ...] = Field(min_length=1)
    resources: tuple[str, ...] = Field(min_length=1)
    maximum_value: float | None = Field(default=None, ge=0)
    maximum_risk: RiskTier = RiskTier.R2
    maximum_delegation_depth: int = Field(default=0, ge=0)

    @field_validator("effects", "resources")
    @classmethod
    def nonblank_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values) or len(values) != len(
            set(values)
        ):
            raise ValueError("capability values must be nonblank and unique")
        return tuple(sorted(values))


class PassportClaims(BaseModel):
    """Signed, minimized delegation claims for one expected invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    credential_id: str = Field(default_factory=generate_uuidv7)
    parent_credential_id: str | None = None
    principal_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    audience: str = Field(min_length=1, max_length=256)
    environment: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    capability: PassportCapability
    invocation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("claim timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_lifetime(self) -> PassportClaims:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self


class SignedPassportCredential(BaseModel):
    """Credential bytes plus issuer metadata required for local verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["fliorcie-passport-local/0.1"] = "fliorcie-passport-local/0.1"
    issuer_id: str = Field(min_length=1, max_length=256)
    key_id: str = Field(min_length=1, max_length=256)
    claims: PassportClaims
    signature: str = Field(min_length=1)

    def signing_payload(self) -> str:
        return canonical_json(
            {
                "profile": self.profile,
                "issuer_id": self.issuer_id,
                "key_id": self.key_id,
                "claims": self.claims.model_dump(mode="json"),
            }
        )


class PassportInvocation(BaseModel):
    """The exact action context that a delegation must bind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    audience: str = Field(min_length=1, max_length=256)
    environment: str = Field(min_length=1, max_length=128)
    effect_name: str = Field(min_length=1, max_length=256)
    resource: str = Field(min_length=1, max_length=512)
    value: float | None = Field(default=None, ge=0)
    risk_tier: RiskTier
    nonce: str = Field(min_length=16, max_length=512)
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def digest(self) -> str:
        return sha256_ref(canonical_json(self.model_dump(mode="json")))


class PassportTrustAnchor(BaseModel):
    """Application-owned issuer key trust; this is not organization proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_id: str = Field(min_length=1, max_length=256)
    key_id: str = Field(min_length=1, max_length=256)
    public_key_pem: str = Field(min_length=1)


class PassportStatus(BaseModel):
    """Caller-supplied current status for one credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active: bool
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("checked_at must include a timezone")
        return value


class PassportVerification(BaseModel):
    """A bounded result that can only narrow an existing Runtime policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str
    delegation_depth: int = Field(ge=0)
    evidence: EvidenceReference | None = None
    limitations: str = _LIMITATIONS


class PassportIssuer:
    """Signs local reference credentials; applications retain their own keys."""

    def __init__(self, issuer_id: str, key_id: str, signing_key: Ed25519KeyManager):
        if not issuer_id.strip() or not key_id.strip():
            raise ValueError("issuer_id and key_id must not be blank")
        self.issuer_id = issuer_id
        self.key_id = key_id
        self.signing_key = signing_key

    def issue(self, claims: PassportClaims) -> SignedPassportCredential:
        unsigned = {
            "profile": "fliorcie-passport-local/0.1",
            "issuer_id": self.issuer_id,
            "key_id": self.key_id,
            "claims": claims.model_dump(mode="json"),
        }
        payload = canonical_json(unsigned)
        return SignedPassportCredential(
            issuer_id=self.issuer_id,
            key_id=self.key_id,
            claims=claims,
            signature=self.signing_key.sign(payload),
        )


StatusResolver = Callable[[SignedPassportCredential], PassportStatus | None]


class ReplayGuard:
    """Process-local nonce reservation port; durable stores can replace it."""

    def reserve(self, scope: str, expires_at: datetime, now: datetime) -> bool:
        raise NotImplementedError


class InMemoryNonceReplayGuard(ReplayGuard):
    """Local development replay guard; process loss is an explicit limitation."""

    def __init__(self) -> None:
        self._entries: dict[str, datetime] = {}
        self._lock = Lock()

    def reserve(self, scope: str, expires_at: datetime, now: datetime) -> bool:
        with self._lock:
            self._entries = {
                item_scope: expiry
                for item_scope, expiry in self._entries.items()
                if expiry > now
            }
            if scope in self._entries:
                return False
            self._entries[scope] = expires_at
            return True


class PassportVerifier:
    """Verifies a supplied local chain without calling Runtime or providers."""

    def __init__(
        self,
        *,
        maximum_status_age: timedelta = timedelta(minutes=5),
        replay_guard: ReplayGuard | None = None,
    ):
        if maximum_status_age < timedelta(0):
            raise ValueError("maximum_status_age must be non-negative")
        self.maximum_status_age = maximum_status_age
        self.replay_guard = replay_guard

    def verify(
        self,
        chain: Sequence[SignedPassportCredential],
        invocation: PassportInvocation,
        trust_anchors: Mapping[tuple[str, str], PassportTrustAnchor],
        status_resolver: StatusResolver,
        *,
        now: datetime | None = None,
    ) -> PassportVerification:
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None:
            raise ValueError("now must include a timezone")
        if not chain:
            return self._deny("credential chain is required")
        previous: SignedPassportCredential | None = None
        for credential in chain:
            failed = self._verify_one(
                credential, invocation, trust_anchors, status_resolver, current
            )
            if failed is not None:
                return self._deny(failed)
            if previous is None:
                if credential.claims.parent_credential_id is not None:
                    return self._deny("root credential must not declare a parent")
            else:
                if (
                    credential.claims.parent_credential_id
                    != previous.claims.credential_id
                ):
                    return self._deny("delegation parent binding does not match")
                if not self._attenuates(previous.claims, credential.claims):
                    return self._deny("child delegation expands parent authority")
            previous = credential
        assert previous is not None
        if len(chain) - 1 > chain[0].claims.capability.maximum_delegation_depth:
            return self._deny("delegation depth exceeds root capability")
        if self.replay_guard is None:
            return self._deny("replay guard is required for a Passport invocation")
        replay_scope = sha256_ref(
            canonical_json(
                {
                    "principal_id": invocation.principal_id,
                    "agent_id": invocation.agent_id,
                    "audience": invocation.audience,
                    "nonce": invocation.nonce,
                }
            )
        )
        if not self.replay_guard.reserve(
            replay_scope, previous.claims.expires_at, current
        ):
            return self._deny("invocation nonce has already been used")
        evidence = EvidenceReference(
            kind="fliorcie-passport-local-delegation",
            sha256=sha256_ref(
                canonical_json([item.model_dump(mode="json") for item in chain])
            ),
            observed_at=current,
        )
        return PassportVerification(
            allowed=True,
            reason="configured issuer, status, binding, and attenuation checks passed",
            delegation_depth=len(chain) - 1,
            evidence=evidence,
        )

    def _verify_one(
        self,
        credential: SignedPassportCredential,
        invocation: PassportInvocation,
        anchors: Mapping[tuple[str, str], PassportTrustAnchor],
        status_resolver: StatusResolver,
        now: datetime,
    ) -> str | None:
        anchor = anchors.get((credential.issuer_id, credential.key_id))
        if anchor is None:
            return "issuer key is not trusted by the application"
        if not Ed25519KeyManager(public_key_pem=anchor.public_key_pem).verify(
            credential.signing_payload(), credential.signature
        ):
            return "credential signature is invalid"
        claims = credential.claims
        if (
            claims.principal_id != invocation.principal_id
            or claims.agent_id != invocation.agent_id
        ):
            return "principal or agent binding does not match"
        if (
            claims.audience != invocation.audience
            or claims.environment != invocation.environment
        ):
            return "audience or environment binding does not match"
        if claims.invocation_hash != invocation.digest():
            return "invocation binding does not match"
        if now < claims.issued_at or now >= claims.expires_at:
            return "credential is outside its validity interval"
        status = status_resolver(credential)
        if status is None or not status.active:
            return "credential status is unavailable or inactive"
        age = now - status.checked_at
        if age < timedelta(0) or age > self.maximum_status_age:
            return "credential status is stale or clock-invalid"
        cap = claims.capability
        if (
            invocation.effect_name not in cap.effects
            or invocation.resource not in cap.resources
        ):
            return "effect or resource is outside delegated capability"
        if cap.maximum_value is not None and (
            invocation.value is None or invocation.value > cap.maximum_value
        ):
            return "invocation value exceeds delegated capability"
        if _RISK_ORDER[invocation.risk_tier] > _RISK_ORDER[cap.maximum_risk]:
            return "invocation risk exceeds delegated capability"
        return None

    @staticmethod
    def _attenuates(parent: PassportClaims, child: PassportClaims) -> bool:
        parent_cap = parent.capability
        child_cap = child.capability
        if not set(child_cap.effects).issubset(parent_cap.effects):
            return False
        if not set(child_cap.resources).issubset(parent_cap.resources):
            return False
        if parent_cap.maximum_value is not None and (
            child_cap.maximum_value is None
            or child_cap.maximum_value > parent_cap.maximum_value
        ):
            return False
        if _RISK_ORDER[child_cap.maximum_risk] > _RISK_ORDER[parent_cap.maximum_risk]:
            return False
        if child_cap.maximum_delegation_depth > parent_cap.maximum_delegation_depth:
            return False
        return child.expires_at <= parent.expires_at

    @staticmethod
    def _deny(reason: str) -> PassportVerification:
        return PassportVerification(allowed=False, reason=reason, delegation_depth=0)


class PassportAuthorityMapper:
    """Intersects verified Passport evidence with the current Runtime policy."""

    def __init__(self, authority_engine: AuthorityEngine | None = None):
        self.authority_engine = authority_engine or AuthorityEngine()

    def evaluate(
        self,
        verification: PassportVerification,
        policy: AuthorityPolicy,
        invocation: PassportInvocation,
        *,
        approval_id: str | None = None,
    ) -> AuthorityResult:
        if not verification.allowed:
            return AuthorityResult(
                policy.policy_id,
                AuthorityDecision.DENY,
                invocation.risk_tier,
                f"passport denied: {verification.reason}",
            )
        return self.authority_engine.evaluate(
            policy,
            effect_name=invocation.effect_name,
            principal_id=invocation.principal_id,
            risk_tier=invocation.risk_tier,
            delegation_depth=verification.delegation_depth,
            approval_id=approval_id,
        )


__all__ = [
    "PassportAuthorityMapper",
    "PassportCapability",
    "PassportClaims",
    "PassportInvocation",
    "InMemoryNonceReplayGuard",
    "PassportIssuer",
    "PassportStatus",
    "PassportTrustAnchor",
    "PassportVerification",
    "PassportVerifier",
    "ReplayGuard",
    "SignedPassportCredential",
]
