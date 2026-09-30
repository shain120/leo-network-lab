"""Session-scoped public function-call facade for isolated LEO experiment nodes."""
from __future__ import annotations

import ipaddress
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).with_name('src')
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from leo_network_lab.config import ROLE_IMAGE_MAP
from leo_decisions import DecisionStore
from leo_flows import FlowService
from leo_sns3 import Sns3CapabilityProbe, Sns3ExecutionService
from leo_experiments import ExperimentRegistry, ExperimentSession
from leo_hypatia import HypatiaCapabilityProbe
from leo_hypatia_visualization import (HypatiaRuntimeProbe, HypatiaVisualizationService,
                                       discover_hypatia_network_datasets)
from leo_hypatia_wizard import HypatiaWizardStore
from leo_runtime_jobs import RuntimeJobStore

ROLES = {'satellite', 'ground_station', 'gateway', 'host'}


class Lab:
    """Drafts do not import Docker or instantiate Containernet."""

    def __init__(self, current_mode: str = 'LEGACY_NETWORK') -> None:
        self.drafts: dict[str, dict[str, str]] = {}
        self.link_drafts: dict[str, dict[str, str]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.active_links: list[dict[str, Any]] = []
        self.backend: Any = None
        self.closed = False
        self._operation_proposals: dict[str, str] = {}
        self._flow_proposals: dict[str, dict[str, Any]] = {}
        self._flow_stop_proposals: dict[str, str] = {}
        self.decisions = DecisionStore()
        self.flows = FlowService(self)
        self.sns3 = Sns3CapabilityProbe()
        self.sns3_execution = Sns3ExecutionService(probe=self.sns3)
        self.experiment_registry = ExperimentRegistry(self.sns3)
        self.experiment_session = ExperimentSession(self.experiment_registry)
        self.hypatia = HypatiaCapabilityProbe()
        runtime_path = Path(__file__).resolve().parent / 'runs/runtime-jobs.json'
        self.runtime_jobs = RuntimeJobStore(runtime_path)
        self.hypatia_visualization = HypatiaVisualizationService(runtime_jobs=self.runtime_jobs)
        self.hypatia_runtime = HypatiaRuntimeProbe(self.hypatia_visualization.config)
        self.hypatia_wizard = HypatiaWizardStore(
            self.hypatia_visualization.config.output_dir / 'hypatia-wizard-state.json',
            self.hypatia_visualization.adapter.capabilities())
        self._satellite_evidence: dict[str, dict[str, Any]] = {}
        self._satellite_findings: list[dict[str, Any]] = []
        self.current_mode = current_mode
        self.current_run_id: str | None = None
        self.last_completed_run_id: str | None = self.sns3_execution.latest_completed_run()

    def __enter__(self) -> Lab:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.hypatia_visualization.close()
            if self.backend is not None:
                self.backend.close()
        finally:
            self.closed = True

    @staticmethod
    def _validate_node(arguments: dict[str, Any]) -> dict[str, str]:
        if set(arguments) != {'name', 'role', 'ip', 'image'}:
            raise ValueError('Required fields: name, role, ip, image')
        if not all(isinstance(v, str) for v in arguments.values()):
            raise ValueError('Node fields must be strings')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', arguments['name']):
            raise ValueError('Invalid node name')
        if arguments['role'] not in ROLES:
            raise ValueError('Unknown node role')
        address = ipaddress.IPv4Interface(arguments['ip'])
        if address.ip.is_loopback or address.ip.is_multicast or address.ip.is_unspecified:
            raise ValueError('Invalid service IP')
        if not arguments['image'] or any(c.isspace() for c in arguments['image']):
            raise ValueError('Invalid image')
        node = {key: str(value) for key, value in arguments.items()}
        node['image'] = ROLE_IMAGE_MAP[node['role']]
        return node

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation_id = uuid.uuid4().hex
        try:
            if self.closed:
                raise ValueError('Lab is closed')
            if not isinstance(arguments, dict):
                raise ValueError('Arguments must be an object')
            if tool == 'get_hypatia_capabilities':
                if arguments: raise ValueError('get_hypatia_capabilities takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.adapter.capabilities()}
            if tool == 'list_hypatia_datasets':
                if arguments: raise ValueError('list_hypatia_datasets takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'datasets': discover_hypatia_network_datasets(self.hypatia_visualization.config)}}
            if tool == 'get_hypatia_dataset':
                if set(arguments) != {'dataset_id'}: raise ValueError('Required: dataset_id')
                dataset = next((item for item in discover_hypatia_network_datasets(self.hypatia_visualization.config)
                                if item['dataset_id'] == arguments['dataset_id']), None)
                if dataset is None: raise ValueError('Hypatia dataset not found')
                return {'operation_id': operation_id, 'status': 'ok', 'data': dataset}
            if tool == 'search_local_ground_station':
                if set(arguments) != {'location'}: raise ValueError('Required: location')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.resolve_ground_station(arguments['location'])}
            if tool == 'resolve_location':
                if set(arguments) != {'location'}: raise ValueError('Required: location')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.resolve_location(arguments['location'])}
            if tool == 'validate_hypatia_config':
                if set(arguments) != {'config'} or not isinstance(arguments['config'], dict):
                    raise ValueError('Required object: config')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.validate_dataset_config(arguments['config'])}
            if tool == 'get_hypatia_wizard_state':
                if arguments: raise ValueError('get_hypatia_wizard_state takes no arguments')
                self.hypatia_wizard.set_available_datasets(
                    discover_hypatia_network_datasets(self.hypatia_visualization.config))
                return {'operation_id': operation_id, 'status': 'ok', 'data': self.hypatia_wizard.snapshot()}
            if tool == 'start_hypatia_wizard':
                if set(arguments) - {'mode', 'intent'} or 'mode' not in arguments:
                    raise ValueError('Required: mode; optional object intent')
                intent = arguments.get('intent')
                if intent is not None and not isinstance(intent, dict): raise ValueError('intent must be an object')
                selected = self.hypatia_visualization.get_selected_network_dataset().get('dataset')
                datasets = discover_hypatia_network_datasets(self.hypatia_visualization.config)
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_wizard.begin(arguments['mode'], intent=intent,
                                                          selected_dataset=selected, datasets=datasets)}
            if tool == 'answer_hypatia_wizard':
                if set(arguments) != {'value'}: raise ValueError('Required: value')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_wizard.answer(arguments['value'])}
            if tool == 'sync_hypatia_wizard':
                if set(arguments) != {'changes'} or not isinstance(arguments['changes'], dict):
                    raise ValueError('Required object: changes')
                self.hypatia_wizard.set_available_datasets(
                    discover_hypatia_network_datasets(self.hypatia_visualization.config))
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_wizard.sync(arguments['changes'])}
            if tool == 'set_hypatia_playback_state':
                allowed = {'enabled', 'start_ms', 'end_ms', 'step_ms', 'speed'}
                if set(arguments) - allowed or not arguments:
                    raise ValueError('Provide one or more playback fields')
                mapping = {'enabled': 'playback_enabled', 'start_ms': 'playback_start_ms',
                           'end_ms': 'playback_end_ms', 'step_ms': 'playback_step_ms',
                           'speed': 'playback_speed'}
                state = self.hypatia_wizard.sync({mapping[key]: value for key, value in arguments.items()})
                return {'operation_id': operation_id, 'status': 'ok', 'data': state}
            if tool == 'get_selected_hypatia_dataset':
                if arguments: raise ValueError('get_selected_hypatia_dataset takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.get_selected_network_dataset()}
            if tool == 'check_ground_station':
                if set(arguments) != {'dataset_id', 'location'}: raise ValueError('Required: dataset_id, location')
                data = self.hypatia_visualization.check_ground_station(**arguments)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'resolve_ground_station':
                if set(arguments) != {'location'}: raise ValueError('Required: location')
                data = self.hypatia_visualization.resolve_ground_station(arguments['location'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'resolve_location':
                if set(arguments) != {'location'}: raise ValueError('Required: location')
                data = self.hypatia_visualization.resolve_location(arguments['location'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'create_custom_ground_station_set':
                if set(arguments) != {'base_dataset_id', 'stations'}: raise ValueError('Required: base_dataset_id, stations')
                data = self.hypatia_visualization.create_custom_ground_station_set(**arguments)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'add_custom_ground_station':
                if set(arguments) != {'custom_set_id', 'name', 'latitude', 'longitude'}: raise ValueError('Required: custom_set_id, name, latitude, longitude')
                data = self.hypatia_visualization.add_custom_ground_station(**arguments)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'generate_hypatia_dataset':
                required = {'network', 'duration_sec', 'step_ms', 'isl_mode', 'ground_station_set', 'routing_algorithm', 'threads'}
                if set(arguments) - (required | {'force_new_dataset'}) or not required <= set(arguments): raise ValueError('Required complete Hypatia dataset configuration; optional force_new_dataset')
                data = self.hypatia_visualization.generate_network_dataset(**arguments)
                if data.get('dataset_id'):
                    dataset = next((item for item in discover_hypatia_network_datasets(self.hypatia_visualization.config)
                                    if item['dataset_id'] == data['dataset_id']), None)
                    if dataset:
                        self.hypatia_visualization.selected_network_dataset_id = dataset['dataset_id']
                        self.hypatia_wizard.set_dataset(dataset)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'get_hypatia_job_status':
                if set(arguments) != {'job_id'}: raise ValueError('Required: job_id')
                data = self.hypatia_visualization.status(arguments['job_id'])
                if data.get('status') == 'COMPLETED' and data.get('dataset_generation') and data.get('dataset_id'):
                    dataset = next((item for item in discover_hypatia_network_datasets(self.hypatia_visualization.config)
                                    if item['dataset_id'] == data['dataset_id']), None)
                    if dataset:
                        self.hypatia_visualization.selected_network_dataset_id = dataset['dataset_id']
                        self.hypatia_wizard.set_dataset(dataset)
                if data.get('status') == 'COMPLETED' and data.get('orchestration') and data.get('dataset_id'):
                    dataset = next((item for item in discover_hypatia_network_datasets(self.hypatia_visualization.config)
                                    if item['dataset_id'] == data['dataset_id']), None)
                    if dataset:
                        self.hypatia_visualization.selected_network_dataset_id = dataset['dataset_id']
                        self.hypatia_wizard.set_dataset(dataset)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'cancel_hypatia_job':
                if set(arguments) != {'job_id'}: raise ValueError('Required: job_id')
                data = self.hypatia_visualization.cancel(arguments['job_id'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'get_runtime_jobs':
                if set(arguments) - {'system'}: raise ValueError('Optional: system')
                system = arguments.get('system')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'jobs': self.runtime_jobs.list(system=system)}}
            if tool == 'get_runtime_job':
                if set(arguments) != {'job_id'}: raise ValueError('Required: job_id')
                row = self.runtime_jobs.get(arguments['job_id'])
                if row is None: raise ValueError('Runtime job not found')
                return {'operation_id': operation_id, 'status': 'ok', 'data': row}
            if tool == 'analyze_hypatia_path':
                allowed = {'dataset_id', 'source', 'destination', 'gen_time_ms', 'force_new'}
                if set(arguments) - allowed or not {'dataset_id', 'source', 'destination'} <= set(arguments): raise ValueError('Required: dataset_id, source, destination; optional gen_time_ms, force_new')
                datasets = discover_hypatia_network_datasets(self.hypatia_visualization.config)
                self.hypatia_wizard.set_available_datasets(datasets)
                selected_dataset = next((item for item in datasets
                                         if item['dataset_id'] == arguments['dataset_id']
                                         and item.get('status') == 'READY'), None)
                if selected_dataset is None:
                    raise ValueError('A discovered READY Hypatia dataset must be selected before analysis')
                self.hypatia_wizard.set_dataset(selected_dataset)
                if 'gen_time_ms' not in arguments:
                    arguments['gen_time_ms'] = int(self.hypatia_wizard.snapshot().get('gen_time_ms', 0))
                data = self.hypatia_visualization.analyze_existing_dataset(**arguments)
                self.hypatia_wizard.sync({'source': arguments['source'], 'destination': arguments['destination']})
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'create_hypatia_analysis':
                allowed = {'dataset_id', 'source', 'destination', 'gen_time_ms', 'network',
                           'force_new', 'force_new_dataset', 'threads'}
                if set(arguments) - allowed or not {'dataset_id', 'source', 'destination'} <= set(arguments):
                    raise ValueError('Required: dataset_id, source, destination; optional network, gen_time_ms, force_new, force_new_dataset')
                if 'gen_time_ms' not in arguments:
                    arguments['gen_time_ms'] = int(self.hypatia_wizard.snapshot().get('gen_time_ms', 0))
                data = self.hypatia_visualization.create_hypatia_analysis(**arguments)
                self.hypatia_wizard.sync({'source': arguments['source'], 'destination': arguments['destination']})
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'generate_satellite_timeline':
                allowed = {'network', 'source', 'destination', 'duration_sec', 'step_ms', 'isl_mode', 'routing_algorithm'}
                if set(arguments) - allowed or not {'network', 'source', 'destination'} <= set(arguments):
                    raise ValueError('Required: network, source, destination')
                data = self.hypatia_visualization.generate_native_timeline(
                    network=arguments['network'], source=arguments['source'], destination=arguments['destination'],
                    duration_sec=arguments.get('duration_sec', 200), step_ms=arguments.get('step_ms', 100),
                    isl_mode=arguments.get('isl_mode', 'isls_plus_grid'),
                    routing_algorithm=arguments.get('routing_algorithm', 'algorithm_free_one_only_over_isls'))
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'calculate_satellite_route_at_time':
                if set(arguments) != {'network', 'source', 'destination', 'gen_time_ms'}:
                    raise ValueError('Required: network, source, destination, gen_time_ms')
                request = self.hypatia_visualization.current_request
                if not request or 'timeline_cache_key' not in request:
                    data = self.hypatia_visualization.generate_satellite_timeline(network=arguments['network'], source=arguments['source'], destination=arguments['destination'])
                    return {'operation_id': operation_id, 'status': 'ok', 'data': {**data, 'route_status': 'TIMELINE_PREPARING'}}
                path = self.hypatia_visualization.config.output_dir / 'timelines' / request['timeline_cache_key'] / 'timeline.json'
                if not path.is_file():
                    return {'operation_id': operation_id, 'status': 'ok', 'data': {'route_status': 'TIMELINE_PREPARING'}}
                timeline = json.loads(path.read_text())
                at = int(arguments['gen_time_ms']); events = [x for x in timeline['route_events'] if x['start_time_ms'] <= at]
                route = events[-1] if events else {'path': []}; samples = [x for x in timeline['rtt_samples'] if x['time_ms'] <= at]
                observation = {'observation_id': 'obs-route-' + uuid.uuid4().hex[:16], 'gen_time_ms': at,
                               'source': timeline['source']['display_name'], 'destination': timeline['destination']['display_name'],
                               'route': route['path'], 'rtt_ms': samples[-1]['rtt_ms'] if samples else None,
                               'hops': {key: route.get(key, 0) for key in ('route_node_count', 'route_link_count', 'satellite_hop_count')}}
                self._satellite_evidence[observation['observation_id']] = observation
                return {'operation_id': operation_id, 'status': 'ok', 'data': observation}
            if tool == 'get_hypatia_snapshot':
                if set(arguments) != {'run_id', 'gen_time_ms'}:
                    raise ValueError('Required: run_id, gen_time_ms')
                data = self.hypatia_visualization.get_hypatia_snapshot(arguments['run_id'], arguments['gen_time_ms'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'get_hypatia_path_series':
                if set(arguments) != {'analysis_id'}: raise ValueError('Required: analysis_id')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.get_path_series(arguments['analysis_id'])}
            if tool == 'get_hypatia_rtt_series':
                if set(arguments) != {'analysis_id'}: raise ValueError('Required: analysis_id')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.hypatia_visualization.get_rtt_series(arguments['analysis_id'])}
            if tool in {'set_satellite_time', 'play_satellite_timeline', 'pause_satellite_timeline', 'set_satellite_playback_speed'}:
                if tool == 'set_satellite_time':
                    if set(arguments) != {'gen_time_ms'}: raise ValueError('Required: gen_time_ms')
                    data = self.hypatia_visualization.timeline_command('setSimulationTime', ms=arguments['gen_time_ms'])
                elif tool == 'set_satellite_playback_speed':
                    if set(arguments) != {'multiplier'} or arguments['multiplier'] not in {0.5, 1, 2, 5}: raise ValueError('multiplier must be 0.5, 1, 2, or 5')
                    data = self.hypatia_visualization.timeline_command('setPlaybackSpeed', multiplier=arguments['multiplier'])
                else:
                    if arguments: raise ValueError(f'{tool} takes no arguments')
                    data = self.hypatia_visualization.timeline_command('playTimeline' if tool.startswith('play') else 'pauseTimeline')
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool in {'generate_satellite_path', 'show_satellite_path'}:
                allowed = {'constellation', 'source', 'destination', 'dataset_id', 'gen_time_ms', 'isl_mode'}
                has_dataset = isinstance(arguments.get('dataset_id'), str) and bool(arguments['dataset_id'])
                has_endpoints = {'constellation', 'source', 'destination'} <= set(arguments)
                if set(arguments) - allowed or not (has_dataset or has_endpoints):
                    raise ValueError('Required: dataset_id, or constellation + source + destination')
                try:
                    # A route artifact is a result, not a prerequisite.  The
                    # legacy dataset ID path remains available for previously
                    # generated official samples, while endpoint requests
                    # always calculate/cache a new real Hypatia route.
                    data = (self.hypatia_visualization.generate_visualization(**arguments)
                            if has_dataset else self.hypatia_visualization.generate_satellite_path(
                                constellation=arguments['constellation'], source=arguments['source'],
                                destination=arguments['destination'], gen_time_ms=arguments.get('gen_time_ms'),
                                isl_mode=arguments.get('isl_mode', 'isls_plus_grid')))
                except ValueError as error:
                    return {'operation_id': operation_id, 'status': 'error', 'error': str(error),
                            'data': {'code': 'DATA_NOT_AVAILABLE', 'base_dataset': 'installed Hypatia inputs'}}
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'set_satellite_time_legacy':
                if set(arguments) != {'gen_time_ms'}: raise ValueError('Required: gen_time_ms')
                request = self.hypatia_visualization.current_request
                if request is None: raise ValueError('No current Hypatia visualization')
                if 'source_endpoint' in request:
                    data = self.hypatia_visualization.generate_satellite_path(
                        constellation=request['constellation'], source=request['source'], destination=request['destination'],
                        gen_time_ms=arguments['gen_time_ms'], isl_mode=request.get('isl_mode', 'isls_plus_grid'),
                        routing_mode=request.get('routing_mode', 'algorithm_free_one_only_over_isls'))
                else:
                    data = self.hypatia_visualization.generate_visualization(**{**request, **arguments})
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'shift_satellite_time':
                if set(arguments) != {'delta_ms'} or type(arguments['delta_ms']) is not int:
                    raise ValueError('Required integer field: delta_ms')
                request = self.hypatia_visualization.current_request
                if request is None or 'resolved_gen_time_ms' not in request:
                    raise ValueError('No current generated Hypatia route')
                target = request['resolved_gen_time_ms'] + arguments['delta_ms']
                if target < 0: raise ValueError('Resolved GEN_TIME cannot be negative')
                data = self.hypatia_visualization.generate_satellite_path(
                    constellation=request['constellation'], source=request['source'], destination=request['destination'],
                    gen_time_ms=target, isl_mode=request.get('isl_mode', 'isls_plus_grid'),
                    routing_mode=request.get('routing_mode', 'algorithm_free_one_only_over_isls'))
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool in {'focus_satellite_view', 'satellite_view_zoom'}:
                if set(arguments) - {'target', 'level'} or 'target' not in arguments:
                    raise ValueError('Required: target; optional: level')
                data = self.hypatia_visualization.camera(**arguments)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool in {'get_satellite_visualization_job', 'cancel_satellite_visualization_job'}:
                if set(arguments) != {'job_id'}: raise ValueError('Required: job_id')
                service = self.hypatia_visualization
                data = service.status(arguments['job_id']) if tool.startswith('get_') else service.cancel(arguments['job_id'])
                if tool.startswith('get_') and data.get('status') == 'COMPLETED' and isinstance(data.get('observation'), dict):
                    observation = data['observation']
                    self._satellite_evidence[observation['observation_id']] = observation
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'hypatia_visualization_runtime':
                if arguments: raise ValueError('hypatia_visualization_runtime takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok', 'data': self.hypatia_runtime.probe()}
            if tool == 'hypatia_visualization_datasets':
                if arguments: raise ValueError('hypatia_visualization_datasets takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'datasets': self.hypatia_runtime.probe()['datasets'], 'source': 'hypatia'}}
            if tool == 'list_experiments':
                if arguments: raise ValueError('list_experiments takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'experiments': self.experiment_registry.list()}}
            if tool in {'describe_experiment', 'get_experiment_schema'}:
                if set(arguments) != {'experiment_id'}:
                    raise ValueError('Required field: experiment_id')
                definition = self.experiment_registry.get(arguments['experiment_id'])
                if definition is None: raise ValueError('Experiment not found')
                data = (definition if tool == 'describe_experiment' else
                        {'experiment_id': definition['id'], 'parameters': definition['parameters'],
                         'availability': definition['availability']})
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool in {'get_canvas_state', 'get_experiment_config'}:
                if arguments: raise ValueError(f'{tool} takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.experiment_session.snapshot()}
            if tool == 'select_experiment':
                if set(arguments) != {'experiment_id'}:
                    raise ValueError('Required field: experiment_id')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.experiment_session.select(arguments['experiment_id'])}
            if tool == 'sync_experiment_canvas':
                if set(arguments) != {'state'} or not isinstance(arguments['state'], dict):
                    raise ValueError('Required object field: state')
                self.experiment_session.sync_canvas(arguments['state'])
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.experiment_session.snapshot()}
            if tool == 'set_experiment_parameter':
                if set(arguments) != {'experiment_id', 'parameter_id', 'value'}:
                    raise ValueError('Required fields: experiment_id, parameter_id, value')
                if arguments['experiment_id'] != self.experiment_session.experiment_id:
                    self.experiment_session.select(arguments['experiment_id'])
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.experiment_session.set_parameter(arguments['parameter_id'], arguments['value'])}
            if tool == 'set_multiple_experiment_parameters':
                if set(arguments) != {'changes'} or not isinstance(arguments['changes'], dict):
                    raise ValueError('Required object field: changes')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.experiment_session.set_multiple(arguments['changes'])}
            if tool == 'start_experiment_wizard':
                if not {'mode'} <= set(arguments) <= {'mode', 'canvas_state'}:
                    raise ValueError('Required field: mode')
                if isinstance(arguments.get('canvas_state'), dict):
                    self.experiment_session.sync_canvas(arguments['canvas_state'])
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.experiment_session.begin_wizard(arguments['mode'])}
            if tool == 'answer_experiment_wizard':
                if set(arguments) != {'value'} or not isinstance(arguments['value'], str):
                    raise ValueError('Required string field: value')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.experiment_session.answer_wizard(arguments['value'])}
            if tool == 'get_next_parameter':
                if arguments: raise ValueError('get_next_parameter takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.experiment_session.next_prompt()}
            if tool == 'validate_experiment_config':
                if arguments: raise ValueError('validate_experiment_config takes no arguments')
                data = self.experiment_session.validate()
                return {'operation_id': operation_id,
                        'status': 'ok' if data['valid'] else 'error', 'data': data,
                        **({'error': '; '.join(data['errors'])} if not data['valid'] else {})}
            if tool == 'preview_experiment':
                if arguments: raise ValueError('preview_experiment takes no arguments')
                data = self.experiment_session.preview()
                return {'operation_id': operation_id,
                        'status': 'ok' if data['validation']['valid'] else 'error', 'data': data,
                        **({'error': '; '.join(data['validation']['errors'])}
                           if not data['validation']['valid'] else {})}
            if tool == 'run_experiment':
                if (set(arguments) != {'validated_config_id', 'confirm'}
                        or arguments['confirm'] is not True):
                    raise ValueError('Explicit UI confirm: true is required')
                validated = self.experiment_session.validated(arguments['validated_config_id'])
                result = self.sns3_execution.run_validated(validated['experiment_id'], validated['config'])
                for evidence in result.get('data', {}).get('evidence', []):
                    self._satellite_evidence[evidence['evidence_id']] = dict(evidence)
                if result.get('status') == 'ok':
                    self.current_run_id = result['data']['run_id']
                    self.last_completed_run_id = result['data']['run_id']
                return {'operation_id': operation_id, **result}
            if tool == 'get_current_run':
                if arguments: raise ValueError('get_current_run takes no arguments')
                run_id = self.current_run_id or self.last_completed_run_id
                completed_runs = self.sns3_execution.completed_runs()
                previous_run_id = next((item for item in completed_runs
                                        if item != run_id), None)
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'current_mode': self.current_mode, 'current_run_id': self.current_run_id,
                    'last_completed_run_id': self.last_completed_run_id,
                    'previous_completed_run_id': previous_run_id,
                    'run_id': run_id, 'status': 'AVAILABLE' if run_id else 'NO_RUN'}}
            if tool in {'get_run_status', 'get_run_summary'}:
                if set(arguments) != {'run_id'}: raise ValueError('Required field: run_id')
                data = self.sns3_execution.get_run(arguments['run_id'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'get_metric_details':
                if set(arguments) != {'run_id', 'metric_id'}:
                    raise ValueError('Required fields: run_id, metric_id')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.sns3_execution.metric_detail(
                            arguments['run_id'], arguments['metric_id'])}
            if tool in {'query_metric', 'get_metric_from_run'}:
                required = {'run_id', 'metric'} if tool == 'query_metric' else {'run_id', 'metric_query'}
                if set(arguments) != required:
                    raise ValueError('Required fields: run_id and metric query')
                query = arguments.get('metric') or arguments.get('metric_query')
                if not arguments.get('run_id'):
                    return {'operation_id': operation_id, 'status': 'ok', 'data': {
                        'status': 'NO_RUN', 'metric_query': query, 'metrics': []}}
                data = self.sns3_execution.metric_from_run(arguments['run_id'], query)
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'list_detected_metrics':
                if set(arguments) != {'run_id'}: raise ValueError('Required field: run_id')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.sns3_execution.detected_metrics(arguments['run_id'])}
            if tool in {'list_run_artifacts', 'get_artifact_index'}:
                if set(arguments) != {'run_id'}: raise ValueError('Required field: run_id')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'artifacts': self.sns3_execution.artifacts(arguments['run_id'])}}
            if tool == 'search_run_artifacts':
                if set(arguments) != {'run_id', 'query'}:
                    raise ValueError('Required fields: run_id, query')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.sns3_execution.search_artifacts(arguments['run_id'], arguments['query'])}
            if tool == 'inspect_artifact':
                if set(arguments) != {'run_id', 'artifact_id'}:
                    raise ValueError('Required fields: run_id, artifact_id')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.sns3_execution.inspect_artifact(
                            arguments['run_id'], arguments['artifact_id'])}
            if tool == 'query_artifact_data':
                required = {'run_id', 'artifact_id'}
                if not required <= set(arguments) <= required | {'columns', 'filters', 'aggregation'}:
                    raise ValueError('Required fields: run_id, artifact_id')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.sns3_execution.query_artifact_data(
                            arguments['run_id'], arguments['artifact_id'],
                            arguments.get('columns'), arguments.get('filters'),
                            arguments.get('aggregation', 'summary'))}
            if tool == 'get_artifact_preview':
                required = {'run_id', 'artifact_id'}
                if not required <= set(arguments) <= required | {'page', 'page_size'}:
                    raise ValueError('Required fields: run_id, artifact_id')
                return {'operation_id': operation_id, 'status': 'ok', 'data':
                        self.sns3_execution.preview_artifact(arguments['run_id'], arguments['artifact_id'],
                            int(arguments.get('page', 1)), int(arguments.get('page_size', 50)))}
            if tool == 'compare_runs':
                if set(arguments) != {'run_id_a', 'run_id_b'}:
                    raise ValueError('Required fields: run_id_a, run_id_b')
                first = self.sns3_execution.get_run(arguments['run_id_a'])
                second = self.sns3_execution.get_run(arguments['run_id_b'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'run_a': first.get('summary', {}), 'run_b': second.get('summary', {})}}
            if tool == 'create_evidence_finding':
                if set(arguments) != {'run_id', 'finding', 'evidence_ids'}:
                    raise ValueError('Required fields: run_id, finding, evidence_ids')
                if not isinstance(arguments['finding'], str) or not arguments['finding'].strip():
                    raise ValueError('Finding must be non-empty text')
                if not isinstance(arguments['evidence_ids'], list) or not arguments['evidence_ids']:
                    raise ValueError('At least one evidence ID is required')
                records = []
                for evidence_id in arguments['evidence_ids']:
                    evidence = self._satellite_evidence.get(evidence_id)
                    if evidence is None or evidence.get('run_id') != arguments['run_id']:
                        raise ValueError('Evidence does not exist or belongs to another run')
                    records.append(evidence)
                finding = {'finding_id': 'finding-' + uuid.uuid4().hex,
                           'run_id': arguments['run_id'],
                           'finding': arguments['finding'].strip(),
                           'evidence_ids': list(dict.fromkeys(arguments['evidence_ids']))}
                self._satellite_findings.append(finding)
                return {'operation_id': operation_id, 'status': 'ok', 'data': {'finding': finding}}
            if tool == 'sns3_capability':
                if arguments:
                    raise ValueError('sns3_capability takes no arguments')
                capability = self.sns3.probe()
                if not capability['available']:
                    reasons = ', '.join(capability['reasons']) or 'unknown reason'
                    return {'operation_id': operation_id, 'status': 'error',
                            'error': f'SNS-3 unavailable: {reasons}', 'data': capability}
                return {'operation_id': operation_id, 'status': 'ok', 'data': capability}
            if tool == 'sns3_experiments':
                if arguments:
                    raise ValueError('sns3_experiments takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'experiments': self.sns3_execution.describe(), 'source': 'sns3'}}
            if tool == 'sns3_scenarios':
                if arguments:
                    raise ValueError('sns3_scenarios takes no arguments')
                capability = self.sns3.probe()
                if not capability['available']:
                    return {'operation_id': operation_id, 'status': 'error',
                            'error': 'SNS-3 unavailable: ' + ', '.join(capability['reasons']),
                            'data': capability}
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'scenarios': self.sns3.scenarios(), 'source': 'sns3'}}
            if tool == 'hypatia_capability':
                if arguments:
                    raise ValueError('hypatia_capability takes no arguments')
                capability = self.hypatia.probe()
                if not capability['available']:
                    return {'operation_id': operation_id, 'status': 'error',
                            'error': 'Hypatia unavailable: ' + ', '.join(capability['reasons']),
                            'data': capability}
                return {'operation_id': operation_id, 'status': 'ok', 'data': capability}
            if tool == 'hypatia_networks':
                if arguments:
                    raise ValueError('hypatia_networks takes no arguments')
                capability = self.hypatia.probe()
                if not capability['available']:
                    return {'operation_id': operation_id, 'status': 'error',
                            'error': 'Hypatia unavailable: ' + ', '.join(capability['reasons']),
                            'data': capability}
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'networks': capability['supported_networks'], 'source': 'hypatia'}}
            if tool == 'run_sns3_experiment':
                if set(arguments) != {'experiment', 'arguments'}:
                    raise ValueError('Required fields: experiment, arguments')
                if not isinstance(arguments['experiment'], str) or not isinstance(arguments['arguments'], dict):
                    raise ValueError('experiment must be a string and arguments must be an object')
                result = self.sns3_execution.run(arguments['experiment'], arguments['arguments'])
                for evidence in result.get('data', {}).get('evidence', []):
                    self._satellite_evidence[evidence['evidence_id']] = dict(evidence)
                return {'operation_id': operation_id, **result}
            if tool == 'get_evidence':
                if set(arguments) != {'evidence_id'} or not isinstance(arguments['evidence_id'], str):
                    raise ValueError('Required field: evidence_id')
                evidence = self._satellite_evidence.get(arguments['evidence_id'])
                if evidence is None:
                    for run_id in filter(None, (self.current_run_id, self.last_completed_run_id)):
                        path = (self.sns3_execution._project_root / 'runs' / run_id /
                                'evidence' / 'evidence.json')
                        try:
                            records = json.loads(path.read_text(encoding='utf-8'))
                        except (OSError, json.JSONDecodeError):
                            continue
                        evidence = next((item for item in records
                                         if item.get('evidence_id') == arguments['evidence_id']), None)
                        if evidence is not None:
                            self._satellite_evidence[arguments['evidence_id']] = evidence
                            break
                if evidence is None:
                    raise ValueError('Evidence not found for the current SNS-3 run')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {'evidence': evidence}}
            if tool == 'propose_flow':
                required = {'source', 'destination', 'protocol', 'rate_mbps', 'duration_seconds'}
                if not required <= set(arguments) <= required | {'port'}:
                    raise ValueError('Invalid flow fields')
                preview = self.flows.preview_flow(**arguments)
                if preview['status'] != 'ready':
                    return {'operation_id': operation_id, **preview}
                proposal_id = uuid.uuid4().hex
                self._flow_proposals[proposal_id] = dict(arguments)
                return {'operation_id': operation_id, 'status': 'draft', 'data': {
                    'proposal': {'proposal_id': proposal_id, 'category': 'network_control',
                                 'kind': 'flow', 'preview': preview}}}
            if tool == 'apply_flow':
                if set(arguments) != {'proposal_id', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit UI confirm: true is required')
                flow_args = self._flow_proposals.pop(arguments['proposal_id'])
                result = self.flows.create_flow(**flow_args)
                return {'operation_id': operation_id, **result}
            if tool == 'flow_status':
                if not set(arguments) <= {'flow_id'}:
                    raise ValueError('Optional field: flow_id')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.flows.status(arguments.get('flow_id'))}
            if tool == 'propose_stop_flow':
                if set(arguments) != {'source'} or arguments['source'] not in self.nodes:
                    raise ValueError('Source node is not a running session-owned node')
                proposal_id = uuid.uuid4().hex
                self._flow_stop_proposals[proposal_id] = arguments['source']
                return {'operation_id': operation_id, 'status': 'draft', 'data': {
                    'proposal': {'proposal_id': proposal_id, 'category': 'network_control',
                                 'kind': 'stop_flow', 'source': arguments['source']}}}
            if tool == 'stop_flow':
                if set(arguments) != {'source', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit confirm: true is required')
                return {'operation_id': operation_id, **self.flows.stop_source_flows(arguments['source'])}
            if tool == 'apply_stop_flow':
                if set(arguments) != {'proposal_id', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit UI confirm: true is required')
                source = self._flow_stop_proposals.pop(arguments['proposal_id'])
                return {'operation_id': operation_id, **self.flows.stop_source_flows(source)}
            if tool == 'record_observation':
                if set(arguments) != {'tool', 'target', 'data'}:
                    raise ValueError('Required fields: tool, target, data')
                observation = self.decisions.record_observation(**arguments)
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'observation': observation}}
            if tool == 'create_finding':
                if set(arguments) != {'type', 'target', 'evidence'}:
                    raise ValueError('Required fields: type, target, evidence')
                finding = self.decisions.create_finding(
                    finding_type=arguments['type'], target=arguments['target'],
                    evidence=arguments['evidence'])
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'finding': finding}}
            if tool == 'create_decision_proposal':
                if set(arguments) != {'goal', 'finding_ids', 'changes'}:
                    raise ValueError('Required fields: goal, finding_ids, changes')
                proposal = self.decisions.create_proposal(
                    goal=arguments['goal'], finding_ids=arguments['finding_ids'],
                    changes=arguments['changes'])
                return {'operation_id': operation_id, 'status': 'draft',
                        'data': {'proposal': proposal}}
            if tool == 'network_state_changed':
                if set(arguments) != {'targets'}:
                    raise ValueError('Required field: targets')
                stale = self.decisions.invalidate(arguments['targets'])
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'stale_decision_ids': stale}}
            if tool == 'get_decision':
                if set(arguments) != {'decision_id'}:
                    raise ValueError('Required field: decision_id')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'decision': self.decisions.get_decision(arguments['decision_id'])}}
            if tool == 'approve_decision':
                if set(arguments) != {'decision_id', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit confirm: true is required')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'decision': self.decisions.approve(arguments['decision_id'])}}
            if tool == 'record_decision_execution':
                if set(arguments) != {'decision_id', 'execution', 'stabilization_seconds'}:
                    raise ValueError('Required fields: decision_id, execution, stabilization_seconds')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'decision': self.decisions.record_execution(**arguments)}}
            if tool == 'record_decision_outcome':
                required = {'decision_id', 'before_observation', 'after_observation',
                            'metric_path', 'direction'}
                if not required <= set(arguments) <= required | {'significance_percent'}:
                    raise ValueError('Invalid outcome fields')
                decision_id = arguments['decision_id']
                outcome_args = {key: value for key, value in arguments.items()
                                if key != 'decision_id'}
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'outcome': self.decisions.record_outcome(decision_id, **outcome_args)}}
            if tool == 'list_decisions':
                if arguments:
                    raise ValueError('list_decisions takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'decisions': self.decisions.list_decisions()}}
            if tool in {'get_node_metrics', 'get_link_metrics'}:
                from leo_telemetry import TelemetryService
                telemetry = TelemetryService(self)
                if tool == 'get_node_metrics':
                    if set(arguments) != {'name'}:
                        raise ValueError('Required field: name')
                    target = {'type': 'node', 'name': arguments['name']}
                    data = telemetry.get_node_metrics(arguments['name'])
                else:
                    if set(arguments) != {'source', 'destination'}:
                        raise ValueError('Required fields: source, destination')
                    target = {'type': 'link', 'source': arguments['source'],
                              'destination': arguments['destination']}
                    data = telemetry.get_link_metrics(arguments['source'], arguments['destination'])
                observation = self.decisions.record_observation(
                    tool=tool, target=target, data=data)
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {**data, 'observation': observation}}
            if tool == 'get_link_packet_snapshot':
                if set(arguments) != {'source', 'destination'}:
                    raise ValueError('Required fields: source, destination')
                from leo_telemetry import TelemetryService
                data = TelemetryService(self).get_link_packet_snapshot(
                    arguments['source'], arguments['destination'])
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            from leo_operations import TOOLS, MUTATIONS, operation_plan, execute_plan
            if tool in TOOLS:
                plan = operation_plan(self, tool, arguments)
                if tool in MUTATIONS:
                    proposal_id = uuid.uuid4().hex
                    self._operation_proposals[proposal_id] = json.dumps(plan)
                    return {'operation_id': operation_id, 'status': 'draft', 'data': {
                        'proposal': {'proposal_id': proposal_id, **plan}}}
                return {'operation_id': operation_id, **execute_plan(self, plan)}
            if tool == 'discard_operation':
                if set(arguments) != {'proposal_id'}:
                    raise ValueError('Required field: proposal_id')
                self._operation_proposals.pop(arguments['proposal_id'], None)
                return {'operation_id': operation_id, 'status': 'ok', 'data': {}}
            if tool == 'apply_operation':
                if set(arguments) != {'proposal_id', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit UI confirm: true is required')
                plan = json.loads(self._operation_proposals.pop(arguments['proposal_id']))
                result = execute_plan(self, plan)
                if result['status'] == 'ok':
                    result.setdefault('data', {})['stale_decision_ids'] = self.decisions.invalidate_all()
                return {'operation_id': operation_id, **result}
            if tool == 'open_terminal':
                if set(arguments) != {'name'}:
                    raise ValueError('Required field: name')
                record = self.nodes[arguments['name']]
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.backend.open_terminal(record['container_id'])}
            if tool == 'execute_safe_node_command':
                if set(arguments) != {'name', 'command_id'}:
                    raise ValueError('Required fields: name, command_id')
                commands = {
                    'show_ip': ['ip', 'addr'],
                    'show_routes': ['ip', 'route'],
                    'show_interfaces': ['ip', '-s', 'link'],
                    'show_tc': ['tc', '-s', 'qdisc', 'show'],
                }
                command = commands.get(arguments['command_id'])
                record = self.nodes.get(arguments['name'])
                if command is None or record is None or self.backend is None:
                    raise ValueError('Unknown safe command or running node')
                result = self.backend.run_probe(record['container_id'], command)
                output = (result.output.decode(errors='replace')
                          if isinstance(result.output, bytes) else str(result.output))
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'node': arguments['name'], 'command_id': arguments['command_id'],
                    'success': result.exit_code == 0, 'output': output}}
            if tool == 'stop':
                if set(arguments) != {'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit confirm: true is required')
                if self.backend is not None:
                    self.backend.close()
                for record in self.nodes.values():
                    self.drafts[uuid.uuid4().hex] = dict(record['spec'])
                for link in self.active_links:
                    self.link_drafts[uuid.uuid4().hex] = {
                        'source': link['source'], 'target': link['target']}
                self.nodes.clear()
                self.active_links.clear()
                self._operation_proposals.clear()
                self.backend = None
                return {'operation_id': operation_id, 'status': 'ok', 'data': {}}
            if tool == 'apply_draft':
                if set(arguments) != {'draft_id', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit confirm: true is required')
                node = self.drafts[arguments['draft_id']]
                if node['name'] in self.nodes:
                    raise ValueError('Node already exists')
                if any(n['spec']['ip'].split('/')[0] == node['ip'].split('/')[0] for n in self.nodes.values()):
                    raise ValueError('Service IP already allocated')
                if self.backend is None:
                    from containernet_backend import ContainernetBackend
                    self.backend = ContainernetBackend()
                cid = self.backend.create(node)
                self.nodes[node['name']] = {'spec': dict(node), 'container_id': cid}
                del self.drafts[arguments['draft_id']]
                try:
                    data = self.backend.query(cid)
                except Exception as exc:
                    return {'operation_id': operation_id, 'status': 'partial',
                            'data': {'name': node['name'], 'container_id': cid,
                                     'state': 'observation_failed'},
                            'error': f'Node created but readback failed: {exc}'}
                return {'operation_id': operation_id, 'status': 'ok', 'data': data}
            if tool == 'discard_draft':
                if set(arguments) != {'draft_id'}:
                    raise ValueError('Required field: draft_id')
                del self.drafts[arguments['draft_id']]
                return {'operation_id': operation_id, 'status': 'ok', 'data': {}}
            if tool == 'delete_node':
                if set(arguments) != {'name', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit confirm: true is required')
                record = self.nodes[arguments['name']]
                self.backend.delete(record['container_id'])
                del self.nodes[arguments['name']]
                return {'operation_id': operation_id, 'status': 'ok', 'data': {}}
            if tool == 'get_workspace':
                if arguments:
                    raise ValueError('get_workspace takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok', 'data': {
                    'drafts': {k: dict(v) for k, v in self.drafts.items()},
                    'link_drafts': {k: dict(v) for k, v in self.link_drafts.items()},
                    'nodes': {k: {'spec': dict(v['spec']), 'container_id': v['container_id']}
                               for k, v in self.nodes.items()},
                    'links': [dict(link) for link in self.active_links]
                }}
            if tool == 'query_node':
                if set(arguments) != {'name'}:
                    raise ValueError('Required field: name')
                record = self.nodes[arguments['name']]
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': self.backend.query(record['container_id'])}
            if tool == 'query_interface_stats':
                if set(arguments) != {'name'}:
                    raise ValueError('Required field: name')
                record = self.nodes.get(arguments['name'])
                if not record:
                    raise ValueError(f"Node not found: {arguments['name']}")
                result = self.backend.run_probe(
                    record['container_id'], ['ip', '-s', '-j', 'link', 'show'])
                output = (result.output.decode(errors='replace')
                          if isinstance(result.output, bytes) else str(result.output))
                if result.exit_code:
                    raise RuntimeError(output or 'Interface statistics query failed')
                interfaces = json.loads(output)
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'interfaces': interfaces}}
            if tool == 'draft_node':
                node = self._validate_node(arguments)
                allocated_names = ({draft['name'] for draft in self.drafts.values()} |
                                   set(self.nodes))
                allocated_ips = ({draft['ip'].split('/')[0]
                                  for draft in self.drafts.values()} |
                                 {record['spec']['ip'].split('/')[0]
                                  for record in self.nodes.values()})
                if node['name'] in allocated_names:
                    raise ValueError('Node already exists')
                if node['ip'].split('/')[0] in allocated_ips:
                    raise ValueError('Service IP already allocated')
                draft_id = uuid.uuid4().hex
                self.drafts[draft_id] = node
                return {'operation_id': operation_id, 'status': 'draft',
                        'data': {'draft_id': draft_id, 'node': dict(node),
                                 'notice': '套用後僅建立隔離 Linux 節點，IP 配置在 lo；無衛星鏈路.'}}

            if tool == 'update_draft':
                if set(arguments) != {'draft_id', 'name', 'role', 'ip', 'image'}:
                    raise ValueError('Required fields: draft_id, name, role, ip, image')
                draft_id = arguments['draft_id']
                if not isinstance(draft_id, str) or draft_id not in self.drafts:
                    raise ValueError('Draft not found')
                node = self._validate_node({key: arguments[key]
                                            for key in ('name', 'role', 'ip', 'image')})
                old_name = self.drafts[draft_id]['name']
                old_ip = self.drafts[draft_id]['ip'].split('/')[0]
                new_ip = node['ip'].split('/')[0]
                if new_ip != old_ip and (
                        any(value['ip'].split('/')[0] == new_ip and key != draft_id
                            for key, value in self.drafts.items()) or
                        any(value['spec']['ip'].split('/')[0] == new_ip
                            for value in self.nodes.values())):
                    raise ValueError('Service IP already allocated')
                if node['name'] != old_name:
                    if node['name'] in self.nodes or any(
                            value['name'] == node['name'] and key != draft_id
                            for key, value in self.drafts.items()):
                        raise ValueError('Node already exists')
                    for link in self.link_drafts.values():
                        if link['source'] == old_name:
                            link['source'] = node['name']
                        if link['target'] == old_name:
                            link['target'] = node['name']
                self.drafts[draft_id] = node
                return {'operation_id': operation_id, 'status': 'draft',
                        'data': {'draft_id': draft_id, 'node': dict(node)}}

            if tool == 'draft_link':
                if set(arguments) != {'source', 'target'}:
                    raise ValueError('Required fields: source, target')
                if arguments['source'] == arguments['target']:
                    raise ValueError('Self-links not supported')
                known_names = ({draft['name'] for draft in self.drafts.values()} |
                               set(self.nodes))
                if arguments['source'] not in known_names or arguments['target'] not in known_names:
                    raise ValueError('Both link endpoints must exist')
                if any({link['source'], link['target']} == {
                        arguments['source'], arguments['target']}
                       for link in [*self.link_drafts.values(), *self.active_links]):
                    raise ValueError('Link already exists')
                link_id = uuid.uuid4().hex
                self.link_drafts[link_id] = dict(arguments)
                return {'operation_id': operation_id, 'status': 'draft',
                        'data': {'link_id': link_id, 'link': dict(arguments)}}

            if tool == 'update_link_draft':
                if set(arguments) != {'link_id', 'source', 'target'}:
                    raise ValueError('Required fields: link_id, source, target')
                link_id = arguments['link_id']
                if not isinstance(link_id, str) or link_id not in self.link_drafts:
                    raise ValueError('Link draft not found')
                source, target = arguments['source'], arguments['target']
                if source == target:
                    raise ValueError('Self-links not supported')
                known_names = ({draft['name'] for draft in self.drafts.values()} |
                               set(self.nodes))
                if source not in known_names or target not in known_names:
                    raise ValueError('Both link endpoints must exist')
                if any(key != link_id and {link['source'], link['target']} == {source, target}
                       for key, link in self.link_drafts.items()):
                    raise ValueError('Link already exists')
                self.link_drafts[link_id] = {'source': source, 'target': target}
                return {'operation_id': operation_id, 'status': 'draft',
                        'data': {'link_id': link_id,
                                 'link': dict(self.link_drafts[link_id])}}

            if tool == 'discard_link':
                if set(arguments) != {'link_id'}:
                    raise ValueError('Required field: link_id')
                if arguments['link_id'] not in self.link_drafts:
                    raise ValueError('Link draft not found')
                del self.link_drafts[arguments['link_id']]
                return {'operation_id': operation_id, 'status': 'ok', 'data': {}}
            if tool == 'list_nodes':
                if arguments:
                    raise ValueError('list_nodes takes no arguments')
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'nodes': list(self.nodes)}}
            if tool == 'validate_topology':
                if arguments:
                    raise ValueError('validate_topology takes no arguments')
                known_names = ({draft['name'] for draft in self.drafts.values()} |
                               set(self.nodes))
                errors = []
                for link_id, link in self.link_drafts.items():
                    missing = [name for name in (link['source'], link['target'])
                               if name not in known_names]
                    if missing:
                        errors.append(f"{link_id}: missing endpoints {', '.join(missing)}")
                return {'operation_id': operation_id,
                        'status': 'ok' if not errors else 'error',
                        'data': {'valid': not errors, 'errors': errors,
                                 'node_count': len(known_names),
                                 'link_count': len(self.link_drafts) + len(self.active_links)}}
            if tool == 'apply_link':
                if set(arguments) != {'link_id', 'confirm'} or arguments['confirm'] is not True:
                    raise ValueError('Explicit confirm: true is required')
                if arguments['link_id'] not in self.link_drafts:
                    raise ValueError('Link draft not found')
                link = self.link_drafts[arguments['link_id']]
                if link['source'] not in self.nodes or link['target'] not in self.nodes:
                    raise ValueError('Both link endpoints must be running nodes')
                if self.backend is None:
                    raise RuntimeError('Backend unavailable for running nodes')
                active = self.backend.create_link(link['source'], link['target'])
                self.active_links.append(dict(active))
                del self.link_drafts[arguments['link_id']]
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'link': dict(active)}}
            if tool == 'ping_test':
                if not {'source', 'target'} <= set(arguments):
                    raise ValueError('Required fields: source, target')
                src_rec = self.nodes.get(arguments['source'])
                if not src_rec:
                    raise ValueError(f"Source node {arguments['source']} not found")

                target = arguments['target']
                tgt_rec = self.nodes.get(target)
                is_external = False
                if not tgt_rec:
                    if re.match(r'^(\d{1,3}\.){3}\d{1,3}$', target):
                        is_external = True
                        tgt_ip = target
                    else:
                        raise ValueError(f"Target node {target} not found and is not a valid IP")
                else:
                    tgt_ip = tgt_rec['spec']['ip'].split('/')[0]

                source = arguments['source']
                if not is_external:
                    adjacency = {name: set() for name in self.nodes}
                    for link in self.active_links:
                        adjacency[link['source']].add(link['target'])
                        adjacency[link['target']].add(link['source'])
                    reachable = {source}
                    pending = [source]
                    while pending:
                        for neighbor in adjacency[pending.pop()]:
                            if neighbor not in reachable:
                                reachable.add(neighbor)
                                pending.append(neighbor)
                    if target not in reachable:
                        return {'operation_id': operation_id, 'status': 'ok',
                                'data': {'success': False, 'reason': 'no_topology_path',
                                         'source': source, 'target': target, 'output': 'No active topology path'}}

                src_cid = src_rec['container_id']
                src_ip = src_rec['spec']['ip'].split('/')[0]
                count = arguments.get('count', 3)
                result = self.backend.run_probe(
                    src_cid, ['ping', '-c', str(count), '-W', '1', '-I', src_ip, tgt_ip])
                output = (result.output.decode(errors='replace')
                          if isinstance(result.output, bytes) else str(result.output))
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'success': result.exit_code == 0,
                                 'reason': ('reachable' if result.exit_code == 0
                                            else 'probe_failed'),
                                 'source': source, 'target': target,
                                 'output': output}}

            if tool == 'run_command':
                if not {'name', 'command'} <= set(arguments) or set(arguments) - {
                        'name', 'command', 'timeout', 'max_output'}:
                    raise ValueError('Required fields: name, command; optional: timeout, max_output')
                command_record = self.nodes.get(arguments['name'])
                if not command_record:
                    raise ValueError(f"Node not found: {arguments['name']}")
                if not isinstance(arguments['command'], str) or not arguments['command'].strip():
                    raise ValueError('Command must be a non-empty string')
                timeout = arguments.get('timeout', 30)
                max_output = arguments.get('max_output', 65536)
                if not isinstance(timeout, int) or not 1 <= timeout <= 300:
                    raise ValueError('Timeout must be an integer from 1 to 300')
                if not isinstance(max_output, int) or not 1024 <= max_output <= 1048576:
                    raise ValueError('max_output must be an integer from 1024 to 1048576')
                result = self.backend.interactive_command(
                    command_record['container_id'], arguments['command'],
                    timeout, max_output)
                output = (result.output.decode(errors='replace')
                          if isinstance(result.output, bytes) else str(result.output))
                return {'operation_id': operation_id, 'status': 'ok',
                        'data': {'success': result.exit_code == 0,
                                 'exit_code': result.exit_code, 'output': output,
                                 'timed_out': bool(result.timed_out),
                                 'truncated': bool(result.truncated)}}
            raise ValueError('Unknown tool or node; apply requires explicit confirmation')
        except Exception as exc:
            return {'operation_id': operation_id, 'status': 'error', 'error': str(exc)}
