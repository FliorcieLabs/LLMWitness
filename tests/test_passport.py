import asyncio
from datetime import datetime, timedelta, timezone

from llmwitness.authority import AuthorityPolicy
from llmwitness.contracts import AgentContract
from llmwitness.effects import EffectContext, SafeEffectRunner
from llmwitness.envelope import (
    Actor,
    AuthorityDecision,
    RiskTier,
    new_trace_id,
    sha256_ref,
)
from llmwitness.journal import SQLiteJournalStore
from llmwitness.passport import (
    InMemoryNonceReplayGuard,
    PassportAuthorityMapper,
    PassportCapability,
    PassportClaims,
    PassportInvocation,
    PassportIssuer,
    PassportStatus,
    PassportTrustAnchor,
    PassportVerifier,
)
from llmwitness.reference_effects import InMemoryRefundEffect
from llmwitness.utils import Ed25519KeyManager, generate_uuidv7

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def _invocation(**changes):
    data = {
        "principal_id": "merchant-user",
        "agent_id": "support-agent",
        "audience": "merchant-api",
        "environment": "staging",
        "effect_name": "reference.refund",
        "resource": "payment-123",
        "value": 25,
        "risk_tier": RiskTier.R2,
        "nonce": "0123456789abcdef",
        "request_hash": sha256_ref("refund-request"),
    }
    data.update(changes)
    return PassportInvocation(**data)


def _issued_chain(*, child=False, **claim_changes):
    key = Ed25519KeyManager()
    issuer = PassportIssuer("merchant-issuer", "key-1", key)
    invocation = _invocation()
    capability = PassportCapability(
        effects=("reference.refund",),
        resources=("payment-123",),
        maximum_value=50,
        maximum_risk=RiskTier.R2,
        maximum_delegation_depth=1,
    )
    data = {
        "principal_id": invocation.principal_id,
        "agent_id": invocation.agent_id,
        "audience": invocation.audience,
        "environment": invocation.environment,
        "policy_version": "policy-v1",
        "capability": capability,
        "invocation_hash": invocation.digest(),
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=10),
    }
    data.update(claim_changes)
    root = issuer.issue(PassportClaims(**data))
    chain = [root]
    if child:
        chain.append(
            issuer.issue(
                PassportClaims(
                    **{
                        **data,
                        "parent_credential_id": root.claims.credential_id,
                        "capability": PassportCapability(
                            effects=("reference.refund",),
                            resources=("payment-123",),
                            maximum_value=25,
                            maximum_risk=RiskTier.R2,
                            maximum_delegation_depth=0,
                        ),
                        "expires_at": NOW + timedelta(minutes=5),
                    }
                )
            )
        )
    anchors = {
        ("merchant-issuer", "key-1"): PassportTrustAnchor(
            issuer_id="merchant-issuer",
            key_id="key-1",
            public_key_pem=key.export_public_key_pem(),
        )
    }
    return invocation, chain, anchors, issuer


def _status(_credential):
    return PassportStatus(active=True, checked_at=NOW)


def _verifier(**kwargs):
    return PassportVerifier(replay_guard=InMemoryNonceReplayGuard(), **kwargs)


def test_local_passport_chain_restricts_runtime_authority_and_records_minimized_evidence(
    tmp_path,
):
    invocation, chain, anchors, _issuer = _issued_chain(child=True)
    verification = _verifier().verify(chain, invocation, anchors, _status, now=NOW)

    assert verification.allowed
    assert verification.delegation_depth == 1
    assert verification.evidence is not None
    assert "payment-123" not in verification.evidence.model_dump_json()
    policy = AuthorityPolicy(
        "runtime-policy",
        frozenset({"reference.refund"}),
        frozenset({"merchant-user"}),
        maximum_risk=RiskTier.R3,
        maximum_delegation_depth=2,
    )
    authority = PassportAuthorityMapper().evaluate(verification, policy, invocation)
    assert authority.decision == AuthorityDecision.ALLOW

    context = EffectContext(
        run_id=generate_uuidv7(),
        trace_id=new_trace_id(),
        actor=Actor(agent_id=invocation.agent_id, principal_id=invocation.principal_id),
        authority=authority,
        contract=AgentContract("refund-contract"),
        authority_evidence=(verification.evidence,),
    )
    with SQLiteJournalStore(tmp_path / "passport.db") as journal:
        execution = asyncio.run(
            SafeEffectRunner(journal).run(
                InMemoryRefundEffect(),
                {"payment_id": "payment-123", "amount": 25},
                context,
            )
        )
        assert execution.state.value == "verified"
        entries = journal.scan(context.run_id)
        assert any(
            item.envelope["evidence"][0]["kind"] == "fliorcie-passport-local-delegation"
            for item in entries
        )


def test_passport_fails_closed_for_signature_binding_and_status_failures():
    invocation, chain, anchors, _issuer = _issued_chain()
    verifier = _verifier(maximum_status_age=timedelta(seconds=30))

    assert (
        not PassportVerifier()
        .verify(chain, invocation, anchors, _status, now=NOW)
        .allowed
    )

    assert (
        not _verifier(maximum_status_age=timedelta(seconds=30))
        .verify(chain, _invocation(audience="other-api"), anchors, _status, now=NOW)
        .allowed
    )
    assert (
        not _verifier(maximum_status_age=timedelta(seconds=30))
        .verify(
            chain,
            invocation,
            {},
            _status,
            now=NOW,
        )
        .allowed
    )
    assert (
        not _verifier(maximum_status_age=timedelta(seconds=30))
        .verify(
            chain,
            invocation,
            anchors,
            lambda _credential: PassportStatus(active=False, checked_at=NOW),
            now=NOW,
        )
        .allowed
    )
    assert not verifier.verify(
        chain,
        invocation,
        anchors,
        lambda _credential: PassportStatus(
            active=True, checked_at=NOW - timedelta(minutes=2)
        ),
        now=NOW,
    ).allowed


def test_passport_rejects_delegation_expansion_replay_and_r3_without_runtime_approval():
    invocation, chain, anchors, issuer = _issued_chain()
    root = chain[0]
    replay_guard = InMemoryNonceReplayGuard()
    replay_verifier = PassportVerifier(replay_guard=replay_guard)
    assert replay_verifier.verify(chain, invocation, anchors, _status, now=NOW).allowed
    assert not replay_verifier.verify(
        chain, invocation, anchors, _status, now=NOW
    ).allowed
    expanded = issuer.issue(
        PassportClaims(
            principal_id=invocation.principal_id,
            agent_id=invocation.agent_id,
            audience=invocation.audience,
            environment=invocation.environment,
            policy_version="policy-v1",
            parent_credential_id=root.claims.credential_id,
            capability=PassportCapability(
                effects=("reference.refund", "reference.delete"),
                resources=("payment-123",),
                maximum_value=100,
                maximum_risk=RiskTier.R3,
                maximum_delegation_depth=1,
            ),
            invocation_hash=invocation.digest(),
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
    )
    assert (
        not _verifier()
        .verify([root, expanded], invocation, anchors, _status, now=NOW)
        .allowed
    )
    assert (
        not _verifier()
        .verify(chain, _invocation(nonce="fedcba9876543210"), anchors, _status, now=NOW)
        .allowed
    )

    high_invocation = _invocation(risk_tier=RiskTier.R3)
    high_claims = root.claims.model_copy(
        update={
            "capability": root.claims.capability.model_copy(
                update={"maximum_risk": RiskTier.R3}
            ),
            "invocation_hash": high_invocation.digest(),
        }
    )
    # Preserve trusted signing for this R3 path.
    trusted_key = Ed25519KeyManager()
    trusted_issuer = PassportIssuer("high-issuer", "key-high", trusted_key)
    high_credential = trusted_issuer.issue(high_claims)
    high_anchor = {
        ("high-issuer", "key-high"): PassportTrustAnchor(
            issuer_id="high-issuer",
            key_id="key-high",
            public_key_pem=trusted_key.export_public_key_pem(),
        )
    }
    verified = _verifier().verify(
        [high_credential], high_invocation, high_anchor, _status, now=NOW
    )
    policy = AuthorityPolicy(
        "p",
        frozenset({"reference.refund"}),
        frozenset({"merchant-user"}),
        maximum_risk=RiskTier.R3,
    )
    assert (
        PassportAuthorityMapper().evaluate(verified, policy, high_invocation).decision
        == AuthorityDecision.REQUIRE_APPROVAL
    )
