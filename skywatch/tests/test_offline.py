"""
一時的なオフライン作業 without the screen (core/offline.py and the stores):
reads answered from the cache while a server is down, changes kept to be
sent (records, checked areas, imaging requests, imaging plans), then sent
or thrown away once it answers again, without saving over others' changes.
"""
from dataclasses import replace

from skywatch.core.config import parse_config
from skywatch.core.esri import EsriClient, UnreachableError
from skywatch.core.gpkg import open_local
from skywatch.core.obs.book import ObservationBook, ObservedImage, Prefs
from skywatch.core.obs.drafts import OutboxDrafts
from skywatch.core.obs.model import ObservationVocabulary, observation_store_of
from skywatch.core.obs.offline_stores import OfflineObservationStore
from skywatch.core.obs.setup import store_sets
from skywatch.core.obs.stores import EsriObservationStore
from skywatch.core.offline import OfflineWork, cache_key, host_of, is_edit
from skywatch.core.offline_upload import upload_all
from skywatch.core.request.store import delete_request, load_requests, request_store_of, save_request, waiting_ids
from skywatch.tests.fake_esri import FakeEsri, FakeLayer
from skywatch.tests.test_obs import ALL, DATES, URL, day, obs
from skywatch.tests.test_request import LEDGER, request, server


class Switch:
    """A transport that is cut off (UnreachableError) while `down`."""

    def __init__(self, fake):
        self.fake = fake
        self.down = False
        self.asked = 0

    def __call__(self, url, form):
        if self.down:
            raise UnreachableError(f"接続不可（{url}）")
        self.asked += 1
        return self.fake(url, form)


def client_of(work, switch):
    client = EsriClient(work.transport(switch))
    client.offline = work
    return client


def obs_layer():
    fake = FakeEsri()
    fake.add_layer(URL, FakeLayer({n: DATES.get(n, "esriFieldTypeString") for n in ALL + ["CONFIDENCE"]}))
    return fake


def test_reads_come_from_the_cache_while_the_server_is_down(tmp_path):
    fake = obs_layer()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    client = client_of(work, switch)
    assert client.query_all(URL, "1=1") == []
    switch.down = True
    # The same read, answered from the cache (the server marked down on the first failure).
    assert client.query_all(URL, "1=1") == []
    assert work.down(URL) and work.offline and host_of(URL) in work.down_hosts
    asked = switch.asked
    try:
        client.query_all(URL, "OBS_ID = 'x'")
        raise AssertionError("no cache for this read")
    except UnreachableError as error:
        assert "控えなし" in str(error)
    assert switch.asked == asked  # not asked again while down
    # The token is not part of what is kept.
    assert cache_key(URL, {"where": "1=1", "token": "a"}) == cache_key(URL, {"where": "1=1", "token": "b"})
    assert work.probe(switch) == [] and work.offline
    switch.down = False
    assert work.probe(switch) == [host_of(URL)] and not work.offline


def test_records_made_offline_wait_and_are_sent_without_overwriting_others(tmp_path):
    fake = obs_layer()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    config = observation_store_of({"label": "共有", "url": URL}, [], 0)
    theirs = EsriObservationStore(config, EsriClient(fake))
    theirs.save([obs(id="a", note=""), obs(id="b", note="")])
    store = OfflineObservationStore(EsriObservationStore(config, client_of(work, switch)), work)
    db = open_local(str(tmp_path / "local.gpkg"))
    from skywatch.core.obs.stores import LocalAreaStore, LocalTaxonomyStore
    prefs = Prefs()
    book = ObservationBook(store, LocalAreaStore(db, "共有"), LocalTaxonomyStore(db, "共有"), ObservationVocabulary(),
                           observer=lambda: "田中", prefs=prefs)
    book.reload()
    book.set_open_images([ObservedImage("img", "img.tif", day(1), None, "L1")], "L1")
    assert len(book.living()) == 2

    switch.down = True
    out = book.update_many(["a", "b"], confidence="確定")
    assert len(out.saved) == 2 and not out.failed
    store.save([obs(id="c", object_id="C", object_name="船舶-3")])
    assert store.pending_ids() == {"a", "b", "c"}
    assert work.counts() == {"observation": 3}
    # Read again while down: the cache with what waits over it.
    book.reload()
    here = {o.id: o for o in book.records}
    assert set(here) == {"a", "b", "c"} and here["a"].confidence == "確定"
    # Someone else changes b on the server meanwhile.
    b = next(o for o in theirs.load() if o.id == "b")
    theirs.save([replace(b, note="ほかの人", updated_at=day(25))])

    switch.down = False
    assert work.probe(switch)
    cfg, _ = parse_config({"observationStores": [{"label": "共有", "url": URL}], "localObservations": False})
    report = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, prefs, lambda: db, now=lambda: day(26))
    server = {o.id: o for o in theirs.load()}
    assert (server["a"].confidence, server["c"].object_name) == ("確定", "船舶-3")
    assert (server["b"].confidence, server["b"].note) == ("", "ほかの人")
    assert server["a"].updated_at == day(26)  # sent now: others' refresh sees it
    # b waits in the same outbox, to be looked at (要確認): not sent again by itself.
    assert report.sent == {"observation": 2} and report.drafts == 1 and work.counts() == {"draft": 1}
    drafts = OutboxDrafts(work).all("共有")
    assert [d.id for d in drafts] == ["b"] and drafts[0].editing and "ほかの人が先に更新" in drafts[0].reason
    assert drafts[0].needs_check and work.sendable() == 0 and work.pending("draft")[0].op == "check"
    assert "送信待ちに残しました" in report.text()
    again = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, prefs, lambda: db, now=lambda: day(27))
    assert again.sent == {} and work.counts() == {"draft": 1}
    # Chosen in the list: tried, and still not saved over the other's change.
    chosen = {("draft", "共有", "b")}
    again = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, prefs, lambda: db, now=lambda: day(28),
                       only=chosen)
    assert again.sent == {} and again.drafts == 1 and work.counts() == {"draft": 1}
    assert next(o for o in theirs.load() if o.id == "b").note == "ほかの人"
    db.close()
    work.close()


def test_deletes_made_offline_are_sent_and_new_ones_just_forgotten(tmp_path):
    fake = obs_layer()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    config = observation_store_of({"label": "共有", "url": URL, "softDelete": False}, [], 0)
    EsriObservationStore(config, EsriClient(fake)).save([obs(id="a")])
    store = OfflineObservationStore(EsriObservationStore(config, client_of(work, switch)), work)
    assert [o.id for o in store.load()] == ["a"]
    switch.down = True
    new = obs(id="n")
    store.save([new])
    assert store.remove([new]) == [] and work.total() == 0
    assert store.removes_for_good()
    store.remove([o for o in store.load() if o.id == "a"])
    assert store.load() == [] and work.counts() == {"observation": 1}
    switch.down = False
    work.probe(switch)
    cfg, _ = parse_config({"observationStores": [{"label": "共有", "url": URL, "softDelete": False}]})
    db = open_local(str(tmp_path / "local.gpkg"))
    report = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, Prefs(), lambda: db)
    assert fake.layers[URL].rows == [] and report.sent == {"observation": 1} and work.total() == 0
    db.close()


def test_offline_work_is_not_the_local_store_and_is_allowed_without_it(tmp_path):
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    db = open_local(str(tmp_path / "local.gpkg"))
    cfg, _ = parse_config({"observationStores": [{"label": "共有", "url": URL}], "localObservations": False})
    sets = store_sets(cfg.observation_stores, cfg.observation_vocabulary, lambda: db,
                      lambda: client_of(work, Switch(obs_layer())), lambda url, token: token, local=cfg.local_observations)
    assert [s.label for s in sets] == ["共有"]
    assert isinstance(sets[0].records, OfflineObservationStore)
    # Without the offline work (tests, scripts), the plain store as before.
    plain = store_sets(cfg.observation_stores, cfg.observation_vocabulary, lambda: db, lambda: EsriClient(obs_layer()),
                       lambda url, token: token, local=False)
    assert isinstance(plain[0].records, EsriObservationStore)
    db.close()


def test_requests_saved_and_deleted_offline_are_sent(tmp_path):
    fake = server()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = request_store_of({"label": "依頼", "url": LEDGER}, [], 0)
    client = client_of(work, switch)
    kept = save_request(client, store, None, request("R1", name="東京"), 1000)
    save_request(client, store, None, request("R2", name="大阪"), 1000)
    loaded = {r.id: r for r in load_requests(client, store, None)}
    switch.down = True
    changed = replace(loaded["R1"], priority=100)
    save_request(client, store, None, changed, 2000)
    new = save_request(client, store, None, request("R3", name="名古屋"), 2000)
    assert new.object_id is None
    delete_request(client, store, None, loaded["R2"])
    assert waiting_ids(client, store) == {"R1", "R2", "R3"}
    shown = {r.id: r for r in load_requests(client, store, None)}
    assert set(shown) == {"R1", "R3"} and shown["R1"].priority == 100
    switch.down = False
    work.probe(switch)
    cfg, _ = parse_config({"requestStores": [{"label": "依頼", "url": LEDGER}]})
    report = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, Prefs(), lambda: None)
    assert report.sent == {"request": 3} and work.total() == 0
    online = {r.id: r for r in load_requests(EsriClient(fake), store, None)}
    assert set(online) == {"R1", "R3"} and online["R1"].priority == 100
    assert kept.object_id is not None


def test_a_request_changed_by_someone_else_meanwhile_stays_waiting(tmp_path):
    fake = server()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = request_store_of({"label": "依頼", "url": LEDGER}, [], 0)
    client = client_of(work, switch)
    save_request(client, store, None, request("R1", name="東京"), 1000)
    mine = load_requests(client, store, None)[0]
    switch.down = True
    save_request(client, store, None, replace(mine, priority=100), 2000)
    theirs = load_requests(EsriClient(fake), store, None)[0]
    save_request(EsriClient(fake), store, None, replace(theirs, priority=900), 3000)
    switch.down = False
    work.probe(switch)
    cfg, _ = parse_config({"requestStores": [{"label": "依頼", "url": LEDGER}]})
    report = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, Prefs(), lambda: None)
    assert report.sent == {} and len(report.kept) == 1 and "ほかの人" in report.kept[0]
    assert load_requests(EsriClient(fake), store, None)[0].priority == 900
    assert work.pending("request")[0].note
    assert work.discard() == 1 and work.total() == 0


def test_plans_an_older_version_saved_offline_are_kept_and_said_so(tmp_path):
    # 撮像計画の保存 is gone (2026-11-09): what is still waiting is neither sent nor lost.
    fake = FakeEsri()
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    work.keep("plan", "計画", "P1", "add", {"attributes": {"PLAN_ID": "P1"}})
    report, _ = _upload(work, Switch(fake))
    assert report.sent == {} and work.counts() == {"plan": 1}
    assert report.kept == ["撮像計画の保存はなくなった（撮像依頼に一本化）ため送らない「計画」の撮像計画 1 件"]


def test_the_settings_key(tmp_path):
    cfg, problems = parse_config({"offlineWork": {"enabled": False, "cacheMB": 50, "probeSeconds": 1}})
    assert (cfg.offline_work.enabled, cfg.offline_work.cache_mb) == (False, 50)
    assert problems == ["offlineWork.probeSeconds は 5 以上の数で指定"]
    assert parse_config({"offlineWork": True})[0].offline_work.enabled
    # Off: nothing is kept, and a server down fails as before.
    work = OfflineWork(str(tmp_path / "offline.sqlite"), enabled=False)
    switch = Switch(obs_layer())
    client = client_of(work, switch)
    client.query_all(URL, "1=1")
    switch.down = True
    try:
        client.query_all(URL, "1=1")
        raise AssertionError("no cache when off")
    except UnreachableError:
        pass
    assert not work.offline


# ---- Security / bug re-check 2026-11-09 -------------------------------------------------------------

class Lossy(Switch):
    """Edits reach the server but their answer is lost (a timeout) while `losing`: the server holds them."""

    def __init__(self, fake):
        super().__init__(fake)
        self.losing = False

    def __call__(self, url, form):
        body = super().__call__(url, form)
        if self.losing and is_edit(url):
            raise UnreachableError(f"応答なし（タイムアウト）（{url}）")
        return body


def _upload(work, transport, **stores):
    cfg, _ = parse_config({**stores, "localObservations": False})
    db = open_local(str(work.path) + ".gpkg")
    prefs = Prefs()
    try:
        return upload_all(cfg, lambda: client_of(work, transport), lambda url, token: token, prefs, lambda: db, now=lambda: day(30)), prefs
    finally:
        db.close()


def test_a_save_whose_answer_was_lost_is_no_conflict_when_sent(tmp_path):
    # The timeout that starts offline work: the server may have saved the change. Sent later, it is not
    # taken for someone else's change (it was left to be looked at as one before).
    fake = obs_layer()
    lossy = Lossy(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    config = observation_store_of({"label": "共有", "url": URL}, [], 0)
    EsriObservationStore(config, EsriClient(fake)).save([obs(id="a", note="")])
    store = OfflineObservationStore(EsriObservationStore(config, client_of(work, lossy)), work)
    a = store.load()[0]
    lossy.losing = True
    store.save([replace(a, note="私の変更", updated_at=day(25))])
    assert work.offline and store.pending_ids() == {"a"}
    assert work.pending("observation")[0].data["tried"] == [day(25)]
    lossy.losing = False
    work.probe(lossy)
    report, prefs = _upload(work, lossy, observationStores=[{"label": "共有", "url": URL}])
    assert report.sent == {"observation": 1} and report.drafts == 0 and work.total() == 0
    assert OutboxDrafts(work).all("共有") == []
    server = EsriObservationStore(config, EsriClient(fake)).load()
    assert [(o.note, o.updated_at) for o in server] == [("私の変更", day(30))]


def test_a_record_saved_on_the_server_after_its_offline_change_is_not_overwritten_later(tmp_path):
    fake = obs_layer()
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    config = observation_store_of({"label": "共有", "url": URL}, [], 0)
    theirs = EsriObservationStore(config, EsriClient(fake))
    theirs.save([obs(id="a", note="")])
    store = OfflineObservationStore(EsriObservationStore(config, client_of(work, switch)), work)
    a = store.load()[0]
    switch.down = True
    store.save([obs(id="n", object_id="N", object_name="船舶-2", note="オフライン", updated_at=day(21))])
    store.save([replace(a, note="オフライン", updated_at=day(21))])
    assert store.pending_ids() == {"n", "a"}
    switch.down = False
    work.probe(switch)
    # 「後で」: not sent yet, and both records changed again with the server answering.
    shown = {o.id: o for o in store.load()}
    store.save([replace(shown["n"], note="あとで直した", updated_at=day(22)), replace(shown["a"], note="あとで直した", updated_at=day(22))])
    assert store.last_saved == {"n", "a"} and store.last_conflicts == set()
    # What waited is superseded: never sent later over the newer save (it was, for the new record).
    assert work.total() == 0
    report, _ = _upload(work, switch, observationStores=[{"label": "共有", "url": URL}])
    assert report.sent == {} and report.drafts == 0
    assert {o.id: o.note for o in theirs.load()} == {"a": "あとで直した", "n": "あとで直した"}


def test_requests_lost_answers_and_saves_after_coming_back(tmp_path):
    fake = server()
    lossy = Lossy(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = request_store_of({"label": "依頼", "url": LEDGER}, [], 0)
    client = client_of(work, lossy)
    save_request(client, store, None, request("R1", name="東京"), 1000)
    save_request(client, store, None, request("R2", name="大阪"), 1000)
    loaded = {r.id: r for r in load_requests(client, store, None)}
    lossy.losing = True
    save_request(client, store, None, replace(loaded["R1"], priority=100), 2000)
    assert work.offline and work.pending("request")[0].data["tried"] == [2000]
    lossy.losing = False
    work.probe(lossy)
    report, _ = _upload(work, lossy, requestStores=[{"label": "依頼", "url": LEDGER}])
    assert report.sent == {"request": 1} and report.kept == [] and work.total() == 0
    assert {r.id: r.priority for r in load_requests(EsriClient(fake), store, None)}["R1"] == 100

    # Changed offline, then again once the ledger answers (before sending): no false conflict, nothing sent later over it.
    switch = Switch(fake)
    client = client_of(work, switch)
    loaded = {r.id: r for r in load_requests(client, store, None)}
    switch.down = True
    save_request(client, store, None, replace(loaded["R2"], priority=300), 3000)
    save_request(client, store, None, request("R3", name="名古屋"), 3000)
    switch.down = False
    work.probe(switch)
    shown = {r.id: r for r in load_requests(client, store, None)}
    save_request(client, store, None, replace(shown["R2"], priority=400), 4000)
    save_request(client, store, None, replace(shown["R3"], priority=700), 4000)
    assert work.total() == 0
    online = {r.id: r.priority for r in load_requests(EsriClient(fake), store, None)}
    assert (online["R2"], online["R3"]) == (400, 700)

