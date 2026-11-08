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
from skywatch.core.obs.drafts import parse_drafts
from skywatch.core.obs.model import ObservationVocabulary, observation_store_of
from skywatch.core.obs.offline_stores import OfflineObservationStore
from skywatch.core.obs.setup import store_sets
from skywatch.core.obs.stores import EsriObservationStore
from skywatch.core.offline import OfflineWork, cache_key, host_of
from skywatch.core.offline_upload import upload_all
from skywatch.core.plan.store import DEFAULT_PLAN_FIELDS, add_shots, delete_plan, list_plans, load_shots, plan_store_of
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
    assert report.sent == {"observation": 2} and report.drafts == 1 and work.total() == 0
    drafts = parse_drafts(prefs.get("drafts/共有"))
    assert [d.id for d in drafts] == ["b"] and drafts[0].editing and "ほかの人が先に更新" in drafts[0].reason
    assert "未保存の記録" in report.text()
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


PLAN_URL = "https://gis.example/arcgis/rest/services/Plans/FeatureServer/0"


def test_plans_saved_offline_are_listed_and_sent(tmp_path):
    fake = FakeEsri()
    fields = {name: "esriFieldTypeString" for name in DEFAULT_PLAN_FIELDS.values()}
    fields.update(SHOT_TIME="esriFieldTypeDate", CREATED_AT="esriFieldTypeDate")
    fake.add_layer(PLAN_URL, FakeLayer(fields, "esriGeometryPolygon"))
    switch = Switch(fake)
    work = OfflineWork(str(tmp_path / "offline.sqlite"))
    store = plan_store_of({"label": "計画", "url": PLAN_URL}, [], 0)
    client = client_of(work, switch)
    assert list_plans(client, store, None) == []
    switch.down = True
    ring = [[[139.6, 35.6], [139.8, 35.6], [139.8, 35.8], [139.6, 35.8], [139.6, 35.6]]]
    features = [{"attributes": {"PLAN_ID": "P1", "PLAN_NAME": "東京", "SHOT_TIME": day(3) + i, "SATELLITE": "WV-3", "STATUS": "予定",
                                "CREATED_AT": day(2)}, "geometry": {"rings": ring}} for i in range(2)]
    assert add_shots(client, store, None, features) == (2, [])
    plans = list_plans(client, store, None)
    assert [(p.id, p.name, p.shots) for p in plans] == [("P1", "東京", 2)]
    shots = load_shots(client, store, None, "P1")
    assert [s.objectId for s in shots] == [-1, -2] and shots[0].satellite == "WV-3"
    switch.down = False
    work.probe(switch)
    cfg, _ = parse_config({"planStores": [{"label": "計画", "url": PLAN_URL}]})
    report = upload_all(cfg, lambda: client_of(work, switch), lambda url, token: token, Prefs(), lambda: None)
    assert report.sent == {"plan": 1} and len(fake.layers[PLAN_URL].rows) == 2
    # A plan saved offline and deleted again never reaches the server.
    switch.down = True
    work.mark_down(PLAN_URL)
    add_shots(client, store, None, [{**features[0], "attributes": {**features[0]["attributes"], "PLAN_ID": "P2"}}])
    assert delete_plan(client, store, None, "P2") == 1 and work.total() == 0


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
