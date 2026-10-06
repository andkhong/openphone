import pytest

from openmob.accounts import AccountStore, AuthError


def store(tmp_path):
    return AccountStore(tmp_path / "accounts.json")


def test_create_account_and_login(tmp_path):
    s = store(tmp_path)
    account = s.create_account("Me@Example.com ", "password123")
    assert s.verify_login("me@example.com", "password123") == account
    assert s.account_email(account) == "me@example.com"  # normalized


def test_login_rejects_wrong_password_and_unknown_email(tmp_path):
    s = store(tmp_path)
    s.create_account("me@example.com", "password123")
    with pytest.raises(AuthError):
        s.verify_login("me@example.com", "wrong")
    with pytest.raises(AuthError):
        s.verify_login("nobody@example.com", "password123")


def test_weak_password_and_duplicate_email_rejected(tmp_path):
    s = store(tmp_path)
    s.create_account("me@example.com", "password123")
    with pytest.raises(AuthError):
        s.create_account("me@example.com", "password123")
    with pytest.raises(AuthError):
        s.create_account("new@example.com", "short")


def test_sessions(tmp_path):
    s = store(tmp_path)
    account = s.create_account("me@example.com", "password123")
    token = s.create_session(account)
    assert s.account_for_session(token) == account
    assert s.account_for_session("nope") is None
    s.end_session(token)
    assert s.account_for_session(token) is None


def test_pairing_code_binds_phone_only_to_its_own_account(tmp_path):
    s = store(tmp_path)
    owner = s.create_account("owner@example.com", "password123")
    attacker = s.create_account("attacker@example.com", "password123")
    code, _ = s.create_pairing_code(owner)

    # The crux: a different account cannot redeem the owner's code, even with the exact code.
    with pytest.raises(AuthError, match="different account"):
        s.redeem_pairing_code(code, attacker, "dev-1", "Pixel")

    key = s.redeem_pairing_code(code, owner, "dev-1", "Galaxy")
    assert s.verify_device("dev-1", key) == owner


def test_pairing_code_is_single_use_and_expires(tmp_path):
    s = store(tmp_path)
    owner = s.create_account("owner@example.com", "password123")
    code, _ = s.create_pairing_code(owner)
    s.redeem_pairing_code(code, owner, "dev-1", "Galaxy")
    with pytest.raises(AuthError, match="invalid or has expired"):
        s.redeem_pairing_code(code, owner, "dev-2", "Galaxy")

    code2, _ = s.create_pairing_code(owner)
    s._data["pairings"][code2[:8]]["expires"] = 0  # force-expire
    with pytest.raises(AuthError, match="invalid or has expired"):
        s.redeem_pairing_code(code2, owner, "dev-3", "Galaxy")


def test_new_pairing_code_voids_the_previous_one(tmp_path):
    s = store(tmp_path)
    owner = s.create_account("owner@example.com", "password123")
    first, _ = s.create_pairing_code(owner)
    s.create_pairing_code(owner)
    with pytest.raises(AuthError):
        s.redeem_pairing_code(first, owner, "dev-1", "Galaxy")


def test_device_key_verification_and_revocation(tmp_path):
    s = store(tmp_path)
    owner = s.create_account("owner@example.com", "password123")
    other = s.create_account("other@example.com", "password123")
    code, _ = s.create_pairing_code(owner)
    key = s.redeem_pairing_code(code, owner, "dev-1", "Galaxy")

    assert s.verify_device("dev-1", key) == owner
    with pytest.raises(AuthError):
        s.verify_device("dev-1", "wrong-key")
    with pytest.raises(AuthError):
        s.verify_device("ghost", key)

    assert [d.id for d in s.list_devices(owner)] == ["dev-1"]
    assert s.list_devices(other) == []
    assert s.revoke_device(other, "dev-1") is False  # not your phone
    assert s.revoke_device(owner, "dev-1") is True
    with pytest.raises(AuthError, match="not paired"):
        s.verify_device("dev-1", key)
    assert s.list_devices(owner) == []


def test_repairing_keeps_device_identity_with_a_fresh_key(tmp_path):
    s = store(tmp_path)
    owner = s.create_account("owner@example.com", "password123")
    key1 = s.redeem_pairing_code(s.create_pairing_code(owner)[0], owner, "dev-1", "Galaxy")
    key2 = s.redeem_pairing_code(s.create_pairing_code(owner)[0], owner, "dev-1", "Galaxy")
    assert key1 != key2
    with pytest.raises(AuthError):
        s.verify_device("dev-1", key1)  # old key no longer works
    assert s.verify_device("dev-1", key2) == owner


def test_store_persists_across_reopen(tmp_path):
    s = store(tmp_path)
    account = s.create_account("me@example.com", "password123")
    key = s.redeem_pairing_code(s.create_pairing_code(account)[0], account, "dev-1", "Galaxy")
    reopened = AccountStore(tmp_path / "accounts.json")
    assert reopened.verify_login("me@example.com", "password123") == account
    assert reopened.verify_device("dev-1", key) == account


def test_cannot_register_a_phone_that_belongs_to_another_account(tmp_path):
    s = store(tmp_path)
    alice = s.create_account("alice@example.com", "password123")
    bob = s.create_account("bob@example.com", "password123")
    alice_key = s.redeem_pairing_code(s.create_pairing_code(alice)[0], alice, "phone-1", "Galaxy")
    bob_code, _ = s.create_pairing_code(bob)
    with pytest.raises(AuthError, match="another account"):
        s.redeem_pairing_code(bob_code, bob, "phone-1", "Galaxy")
    assert s.verify_device("phone-1", alice_key) == alice  # Alice keeps it, key still valid
    # Bob's refused attempt did not burn his code.
    s.redeem_pairing_code(bob_code, bob, "phone-2", "Pixel")
    # Once Alice revokes it, the phone can be registered to Bob, as a fresh record.
    s.revoke_device(alice, "phone-1")
    bob_key = s.redeem_pairing_code(s.create_pairing_code(bob)[0], bob, "phone-1", "Galaxy")
    assert s.verify_device("phone-1", bob_key) == bob
    assert [d.id for d in s.list_devices(alice)] == []


def test_admin_role_defaults_off_and_is_set_by_email(tmp_path):
    s = store(tmp_path)
    account = s.create_account("me@example.com", "password123")
    assert not s.is_admin(account) and not s.is_admin(None) and not s.is_admin("nope")
    assert s.set_admin("ME@example.com", True) and s.is_admin(account)
    assert AccountStore(s.path).is_admin(account)  # persisted
    assert s.set_admin("me@example.com", False) and not s.is_admin(account)
    assert not s.set_admin("nobody@example.com", True)


def test_old_store_without_admin_flag(tmp_path):
    import json

    s = store(tmp_path)
    account = s.create_account("me@example.com", "password123")
    data = json.loads(s.path.read_text())
    data["accounts"][account].pop("is_admin", None)
    s.path.write_text(json.dumps(data))
    s2 = store(tmp_path)
    assert not s2.is_admin(account)
    assert s2.admin_list_accounts()[0]["is_admin"] is False


def test_admin_listings_and_revoke(tmp_path):
    s = store(tmp_path)
    alice = s.create_account("alice@example.com", "password123")
    bob = s.create_account("bob@example.com", "password123")
    code, _ = s.create_pairing_code(bob)
    key = s.redeem_pairing_code(code, bob, "bob-phone", "Pixel")

    accounts = {a["email"]: a for a in s.admin_list_accounts()}
    assert set(accounts["bob@example.com"]) == {"id", "email", "created", "is_admin", "device_count", "api_disabled"}
    assert accounts["bob@example.com"]["device_count"] == 1 and accounts["alice@example.com"]["id"] == alice

    [device] = s.admin_list_devices()
    assert set(device) == {"id", "account_id", "email", "model", "created", "last_seen", "revoked"}
    assert device["id"] == "bob-phone" and device["email"] == "bob@example.com" and not device["revoked"]

    assert not s.admin_revoke_device("nope")
    assert s.admin_revoke_device("bob-phone")  # regardless of owner
    assert s.admin_list_devices()[0]["revoked"] and s.admin_list_accounts()[1]["device_count"] == 0
    with pytest.raises(AuthError):
        s.verify_device("bob-phone", key)
