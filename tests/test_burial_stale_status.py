"""Regression checks for plan status versus persisted bathymetry currency."""
import json
from types import SimpleNamespace

from ..burial import generation, schema
from ..burial.plan_model import PlanModel
from .test_burial_dock_lifecycle import _Harness

REQUIRES_QGIS = True


def _generated(model):
    model.refresh_layers = lambda *a, **k: None
    assert model.apply_generation(generation.GenerationOutput(), model.gen_params(),
                                  [], {}, schema.new_id())


def test_profile_completion_reports_save_failure_and_current_reasons():
    with _Harness() as h:
        dock = h.dock
        model = dock.model
        _generated(model)
        task = SimpleNamespace(cancelled=False, error=None, series=[(0.0, 10.0)],
                               step_m=2.0, cross_offset_m=2.0, kps=[0.0],
                               depths=[10.0], source_ids=[], cell_sizes_m=[],
                               cross_max_deg=[], port_depths=[10.0], stbd_depths=[10.0])
        dock._profile_identity = dict(route_fingerprint='', depth_fingerprint='',
                                     route_geom_fingerprint='', depth_layers={},
                                     depth_signature='', depth_layer_names={})
        displayed = []
        dock._display_stored_profile = lambda *a, **k: displayed.append(k)
        real_save = model.save_profile
        model.save_profile = lambda p: False
        dock._profile_task = task
        dock._profile_finished(task, dock._profile_generation)
        assert not displayed
        assert 'saving failed' in dock.profile_status.text()
        assert 'saving failed' in dock.profile_tab.profile_state_label.text()
        model.save_profile = real_save
        model.profile_stale_reasons = lambda: ['the bathymetry source changed since sampling']
        dock._profile_task = task
        dock._profile_finished(task, dock._profile_generation)
        assert displayed == [{'reasons': ['the bathymetry source changed since sampling']}]
        assert model.bathy_profile.depths == [10.0]
        assert model.plan['status'] == schema.PLAN_STATUS_DRAFT


def _manual_plan(model):
    model.refresh_layers = lambda *a, **k: None
    assert model.update_plan({'scope_start_kp': 0.0, 'scope_end_kp': 4.0})
    assert model.add_event(1.0, schema.EVENT_BURIAL_START, note='Keep this note')
    assert model.add_event(3.0, schema.EVENT_BURIAL_END)



def test_editing_manual_imported_and_generated_plans_needs_no_acknowledgement():
    from ..burial.profile_data import PlanProfile
    for workflow in ('manual', 'imported', 'generated'):
        with _Harness() as h:
            model = h.dock.model
            _manual_plan(model)
            if workflow == 'imported':
                imported = [dict(e, source=schema.EVENT_SOURCE_CLIENT) for e in model.events]
                assert model.import_plan(imported, 'rpl')
            elif workflow == 'generated':
                assert model.apply_generation(
                    generation.GenerationOutput(events=model.events, sections=model.sections),
                    model.gen_params(), [], {}, schema.new_id())
            assert model.save_profile(PlanProfile(kps=[0.0], depths=[10.0]))
            assert model.profile_state() == 'stale'
            events = h.store.list_events(h.plan_id)
            sections = h.store.list_sections(h.plan_id)
            generations = h.store.list_generations(h.plan_id)
            assert model.update_plan({'scope_end_kp': 5.0})
            assert model.update_gen_params({'cross_offset_m': 4.0})
            assert model.save_rules([])
            assert model.plan['status'] == schema.PLAN_STATUS_DRAFT
            h.dock._refresh_strip()
            assert h.dock.status_badge.text() == 'draft'
            assert not hasattr(h.dock.plan_tab, 'review_button')
            assert h.store.list_events(h.plan_id) == events
            assert h.store.list_sections(h.plan_id) == sections
            assert h.store.list_generations(h.plan_id) == generations
            assert model.profile_state() == 'stale'
            again = PlanModel(h.store)
            assert again.load_plan(h.plan_id)
            assert again.plan['status'] == schema.PLAN_STATUS_DRAFT
            assert again.profile_state() == 'stale'
            again.close_plan()


def test_legacy_stale_flag_is_removed_without_changing_plan_or_route_anchor():
    with _Harness() as h:
        model = h.dock.model
        _manual_plan(model)
        model.plan.update(status=schema.PLAN_STATUS_STALE,
                          rpl_fingerprint='rpl|old|lines|path',
                          params_json=json.dumps({'cross_offset_m': 4.0,
                                                  'plan_stale_reasons': ['Survey changed']}))
        h.store.save_plan(model.plan)
        events = h.store.list_events(h.plan_id)
        sections = h.store.list_sections(h.plan_id)
        history = h.store.list_change_log(h.plan_id)
        assert model.load_plan(h.plan_id)
        assert model.plan['status'] == schema.PLAN_STATUS_DRAFT
        assert model.plan['rpl_fingerprint'] == 'rpl|old|lines|path'
        assert json.loads(model.plan['params_json']) == {'cross_offset_m': 4.0}
        assert h.store.get_plan(h.plan_id)['status'] == schema.PLAN_STATUS_DRAFT
        assert h.store.list_events(h.plan_id) == events
        assert h.store.list_sections(h.plan_id) == sections
        assert h.store.list_change_log(h.plan_id) == history
        assert not h.store.list_generations(h.plan_id)


def test_status_cleanup_preserves_issued_plans_and_handles_failed_writes():
    with _Harness() as h:
        model = h.dock.model
        model.plan['status'] = schema.PLAN_STATUS_ISSUED
        model._normalise_plan_status()
        assert model.plan['status'] == schema.PLAN_STATUS_ISSUED
        model.plan['status'] = schema.PLAN_STATUS_STALE
        before = dict(model.plan)
        model._store_write = lambda *a, **k: (False, None)
        model._normalise_plan_status()
        assert model.plan == before
        h.dock._refresh_strip()
        assert h.dock.status_badge.text() == 'draft'
