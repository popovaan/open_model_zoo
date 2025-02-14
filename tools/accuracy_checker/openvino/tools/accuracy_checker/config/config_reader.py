"""
Copyright (c) 2018-2022 Intel Corporation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from argparse import Namespace
import copy
from pathlib import Path
import os

import warnings

from ..utils import read_yaml, to_lower_register, contains_any, is_iterable
from .config_validator import ConfigError

ENTRIES_PATHS = {
    'launchers': {
        'cpu_extensions': 'extensions',
        'gpu_extensions': 'extensions',
        'affinity_map': 'affinity_map',
        'predictions': 'source'
    },
    'datasets': {
        'segmentation_masks_source': 'source',
        'annotation': 'annotations',
        'dataset_meta': 'annotations',
        'data_source': 'source',
        'additional_data_source': 'source',
        "subset_file": "annotations"
    },
}

PREPROCESSING_PATHS = {
    'mask_dir': 'source',
    'vocabulary_file': ['model_attributes', 'models', 'source'],
    'merges_file': ['model_attributes', 'source', 'models']
}

ADAPTERS_PATHS = {
    'lm_file': ['model_attributes', 'models', 'source'],
    'vocabulary_file': ['model_attributes', 'models', 'source'],
    'merges_file': ['model_attributes', 'models', 'source'],
    'fst_file': ['model_attributes', 'models', 'source'],
    'words_file': ['model_attributes', 'models', 'source'],
    'transition_model_file': ['model_attributes', 'models', 'source'],
}

ANNOTATION_CONVERSION_PATHS = {
    'vocab_file': ['model_attributes', 'source', 'models'],
    'merges_file': ['model_attributes', 'source', 'models'],
    'mask_file': ['model_attributes', 'source', 'models'],
    'stats_file': ['model_attributes', 'source', 'models'],
    'tokenizer_dir': ['model_attributes', 'models', 'source']
}

LIST_ENTRIES_PATHS = {
    'model': 'models',
    'weights': 'models',
    'color_coeff': ['model_attributes', 'models'],
    'saved_model_dir': 'models',
    'params': 'models'
}

COMMAND_LINE_ARGS_AS_ENV_VARS = {
    'source': 'DATA_DIR',
    'annotations': 'ANNOTATIONS_DIR',
    'models': 'MODELS_DIR',
    'extensions': 'EXTENSIONS_DIR',
    'model_attributes': 'MODEL_ATTRIBUTES_DIR',
    'kaldi_bin_dir': 'KALDI_BIN_DIR'
}
DEFINITION_ENV_VAR = 'DEFINITIONS_FILE'
CONFIG_SHARED_PARAMETERS = []
ACCEPTABLE_MODEL = [
    'model',
    'saved_model_dir',
    'params'
]
ALLOW_FILE_OR_DIR = ['models']


class ConfigReader:
    """
    Class for parsing input config.
    """

    @staticmethod
    def merge(arguments):
        """
        Args:
            arguments: command-line arguments.
        Returns:
            dictionary containing configuration.
        """

        global_config, local_config = ConfigReader._read_configs(arguments)
        if not local_config:
            raise ConfigError('Missing local config')

        mode = ConfigReader.check_local_config(local_config)
        ConfigReader._prepare_global_configs(global_config)

        config = ConfigReader._merge_configs(global_config, local_config, arguments, mode)
        ConfigReader.process_config(config, mode, arguments)

        return config, mode

    @staticmethod
    def process_config(config, mode='models', arguments=None):
        if arguments is None:
            arguments = {}
        ConfigReader._merge_paths_with_prefixes(arguments, config, mode)
        ConfigReader._provide_cmd_arguments(arguments, config, mode)
        ConfigReader._filter_launchers(config, arguments, mode)
        ConfigReader._separate_evaluations(config, mode)
        ConfigReader._previous_configuration_parameters_sharing(config, mode)

    @staticmethod
    def _read_configs(arguments):
        local_config = read_yaml(arguments.config)
        if not isinstance(local_config, dict):
            raise ConfigError('local config should be dict-like object')
        definitions = os.environ.get(DEFINITION_ENV_VAR) or local_config.get('global_definitions')
        if definitions:
            definitions = read_yaml(Path(arguments.config).parent / definitions)
        global_config = (
            read_yaml(arguments.definitions) if 'definitions' in arguments and arguments.definitions else definitions
        )

        return global_config, local_config

    @staticmethod
    def check_local_config(config):
        def _is_requirements_missed(target, requirements):
            return list(filter(lambda entry: not target.get(entry), requirements))

        def _check_models_config(config):
            models = config.get('models')
            if not models:
                raise ConfigError('Missed "{}" in local config'.format('models'))

            required_model_entries = ['name', 'launchers', 'datasets']
            required_dataset_entries = ['name']
            required_dataset_error = 'Model {} must specify {} for each dataset'
            for model in models:
                if _is_requirements_missed(model, required_model_entries):
                    raise ConfigError('Each model must specify {}'.format(', '.join(required_model_entries)))
                datasets = model['datasets'].values() if isinstance(model['datasets'], dict) else model['datasets']
                if list(filter(lambda entry: _is_requirements_missed(entry, required_dataset_entries), datasets)):
                    raise ConfigError(required_dataset_error.format(model['name'], ', '.join(required_dataset_entries)))

        def _check_module_config(config):
            required_entries = ['name', 'module']
            evaluations = config['evaluations']
            if not evaluations:
                raise ConfigError('Missed "{}" in local config'.format('evaluations'))
            for evaluation in evaluations:
                if _is_requirements_missed(evaluation, required_entries):
                    raise ConfigError('Each evaluations must specify {}'.format(', '.join(required_entries)))

        config_checkers = {
            'evaluations': _check_module_config,
            'models': _check_models_config,
        }

        if not isinstance(config, dict):
            raise ConfigError('local config should has dictionary based structure')

        eval_mode = get_mode(config)
        config_checker_func = config_checkers.get(eval_mode)
        if config_checker_func is None:
            raise ConfigError(
                'Accuracy Checker {} mode is not supported. Please select between evaluations and models.'.format(
                    eval_mode))
        config_checker_func(config)

        return eval_mode

    @staticmethod
    def _prepare_global_configs(global_configs):
        if not global_configs or 'datasets' not in global_configs:
            return

        datasets = global_configs['datasets']

        def merge(local_entries, global_entries, identifier):
            if not local_entries or not global_entries:
                return

            for i, local in enumerate(local_entries):
                local_identifier = local.get(identifier)
                if not local_identifier:
                    continue

                local_entries[i] = ConfigReader._merge_configs_by_identifier(global_entries, local, identifier)

        for dataset in datasets:
            merge(dataset.get('preprocessing'), global_configs.get('preprocessing'), 'type')
            merge(dataset.get('metrics'), global_configs.get('metrics'), 'type')
            merge(dataset.get('postprocessing'), global_configs.get('postprocessing'), 'type')

    @staticmethod
    def _merge_models_config(global_configs, local_config, arguments):
        config = copy.deepcopy(local_config)
        if not global_configs:
            return config

        models = config['models']
        for model in models:
            if 'launchers' in global_configs:
                for i, launcher_entry in enumerate(model['launchers']):
                    model['launchers'][i] = ConfigReader._merge_configs_by_identifier(
                        global_configs['launchers'], launcher_entry, 'framework'
                    )
            if 'datasets' in global_configs:
                datasets_iterator = (
                    model['datasets'].items() if isinstance(model['datasets'], dict)
                    else enumerate(model['datasets'])
                )
                for i, dataset in datasets_iterator:
                    model['datasets'][i] = ConfigReader._merge_configs_by_identifier(
                        global_configs['datasets'], dataset, 'name'
                    )

        config['models'] = models
        return config

    @staticmethod
    def _merge_module_config(global_config, local_config, args):

        config = copy.deepcopy(local_config)
        if not global_config:
            return config

        for evaluation in config['evaluations']:
            if 'module_config' not in evaluation:
                continue
            module_config = evaluation['module_config']
            if 'launchers' in module_config and 'launchers' in global_config:
                for i, launcher_entry in enumerate(module_config['launchers']):
                    module_config['launchers'][i] = ConfigReader._merge_configs_by_identifier(
                        global_config['launchers'], launcher_entry, 'framework'
                    )
            if 'datasets' in module_config and 'datasets' in global_config:
                datasets_iterator = (
                    module_config['datasets'].items() if isinstance(module_config['datasets'], dict)
                    else enumerate(module_config['datasets'])
                )
                for i, dataset in datasets_iterator:
                    module_config['datasets'][i] = ConfigReader._merge_configs_by_identifier(
                        global_config['datasets'], dataset, 'name'
                    )

        return config

    @staticmethod
    def _merge_configs(global_configs, local_config, arguments, mode='models'):
        functors_by_mode = {
            'models': ConfigReader._merge_models_config,
            'evaluations': ConfigReader._merge_module_config
        }

        return functors_by_mode[mode](global_configs, local_config, arguments)

    @staticmethod
    def _merge_configs_by_identifier(global_config, local_config, identifier):
        local_identifier = local_config.get(identifier)
        if local_identifier is None:
            return local_config

        matched = []
        for config in global_config:
            global_identifier = config.get(identifier)
            if global_identifier is None:
                continue

            if global_identifier != local_identifier:
                continue

            matched.append(config)

        config = copy.deepcopy(matched[0] if matched else {})
        for key, value in local_config.items():
            config[key] = value

        return config

    @staticmethod
    def _merge_paths_with_prefixes(arguments, config, mode='models'):
        args = arguments if isinstance(arguments, dict) else vars(arguments)
        for argument, env_var in COMMAND_LINE_ARGS_AS_ENV_VARS.items():
            if argument not in args or args[argument] is None:
                env_var_value = os.environ.get(env_var)
                if env_var_value is not None:
                    args[argument] = Path(env_var_value)

        def process_models(config, entries_paths):
            for model in config['models']:
                process_config(model, entries_paths, args)

        def process_modules(config, entries_paths):
            for evaluation in config['evaluations']:
                module_config = evaluation.get('module_config')
                if not module_config:
                    continue
                process_config(module_config, entries_paths, args)
                if 'network_info' in module_config:
                    networks_info = module_config['network_info']
                    if isinstance(networks_info, dict):
                        for _, params in networks_info.items():
                            entries_paths['launchers'].update(LIST_ENTRIES_PATHS)
                            merge_entry_paths(entries_paths['launchers'], params, args)
                    if isinstance(networks_info, list):
                        merge_entry_paths(entries_paths['launchers'], networks_info, args)

        functors_by_mode = {
            'models': process_models,
            'evaluations': process_modules
        }

        processing_func = functors_by_mode[mode]
        processing_func(config, ENTRIES_PATHS.copy())

    @staticmethod
    def _provide_cmd_arguments(arguments, config, mode):
        profile_dataset = 'profile' in arguments and arguments.profile
        profile_report_type = arguments.profile_report_type if 'profile_report_type' in arguments else 'csv'

        def merge_models(config, arguments, update_launcher_entry):

            for model in config['models']:
                for launcher_entry in model['launchers']:
                    merge_dlsdk_launcher_args(arguments, launcher_entry, update_launcher_entry)
                model['launchers'] = provide_models(model['launchers'], arguments)

                for dataset_entry in model['datasets']:
                    _add_subset_specific_arg(dataset_entry, arguments)

                    if 'ie_preprocessing' in arguments and arguments.ie_preprocessing:
                        dataset_entry['_ie_preprocessing'] = arguments.ie_preprocessing

                    if profile_dataset:
                        dataset_entry['_profile'] = profile_dataset
                        dataset_entry['_report_type'] = profile_report_type

        def merge_modules(config, arguments, update_launcher_entry):
            for evaluation in config['evaluations']:
                module_config = evaluation.get('module_config')
                if not module_config:
                    continue
                if 'models' in arguments and arguments.models:
                    module_config['_models'] = arguments.models
                    if 'model_is_blob' in arguments:
                        module_config['_model_is_blob'] = arguments.model_is_blob
                if 'launchers' not in module_config:
                    continue
                for launcher in module_config['launchers']:
                    merge_dlsdk_launcher_args(arguments, launcher, update_launcher_entry)

                if 'datasets' not in module_config:
                    continue
                for dataset in module_config['datasets']:
                    _add_subset_specific_arg(dataset, arguments)
                    dataset['_profile'] = profile_dataset
                    dataset['_report_type'] = profile_report_type

        functors_by_mode = {
            'models': merge_models,
            'evaluations': merge_modules
        }

        additional_keys = [
            'cpu_extensions_mode', 'vpu_log_level'
        ]
        arguments_dict = arguments if isinstance(arguments, dict) else vars(arguments)
        update_launcher_entry = {}

        for key in additional_keys:
            value = arguments_dict.get(key)
            if value:
                update_launcher_entry['_{}'.format(key)] = value

        return functors_by_mode[mode](config, arguments, update_launcher_entry)

    @staticmethod
    def _filter_launchers(config, arguments, mode='models'):
        functors_by_mode = {
            'models': filter_models,
            'evaluations': filter_modules
        }

        args = arguments if isinstance(arguments, dict) else vars(arguments)
        target_devices = to_lower_register(args.get('target_devices') or [])
        filtering_mode = functors_by_mode[mode]
        filtering_mode(config, target_devices, args)

    @staticmethod
    def _separate_evaluations(config, mode='models'):
        def _separate_models_evaluations(models_config):
            evaluations = []
            for model in models_config['models']:
                launchers = model['launchers']
                datasets = model['datasets']
                if not launchers:
                    continue
                if len(launchers) == 1 and len(datasets) == 1:
                    evaluations.append(model)
                    continue
                for launcher in model['launchers']:
                    model_evaluations = []
                    model_config_copy_launcher = copy.deepcopy(model)
                    model_config_copy_launcher['launchers'] = [launcher]

                    for dataset in model_config_copy_launcher['datasets']:
                        model_config_copy_dataset = copy.deepcopy(model_config_copy_launcher)
                        model_config_copy_dataset['datasets'] = [dataset]
                        model_evaluations.append(model_config_copy_dataset)

                    evaluations.extend(model_evaluations)

            models_config['models'] = evaluations

        def _separate_modules_evaluations(modules_config):
            evals = modules_config['evaluations']
            eval_list = []
            for evaluation in evals:
                if 'module_config' not in evaluation:
                    eval_list.append(evaluation)
                    continue
                module_config = evaluation['module_config']
                launchers = module_config.get('launchers', [])
                datasets = module_config.get('datasets', [])
                eval_config_list = []
                for launcher in launchers:
                    copy_module_config = copy.deepcopy(module_config)
                    copy_module_config['launchers'] = [launcher]
                    if not datasets:
                        eval_config_list.append(copy_module_config)
                        continue
                    for dataset in datasets:
                        copy_evaluation_for_dataset = copy.deepcopy(copy_module_config)
                        copy_evaluation_for_dataset['datasets'] = [dataset]
                        eval_config_list.append(copy_evaluation_for_dataset)
                for eval_config in eval_config_list:
                    copy_evaluation = copy.deepcopy(evaluation)
                    copy_evaluation['module_config'] = eval_config
                    eval_list.append(copy_evaluation)

            modules_config['evaluations'] = eval_list

        mode_func = {
            'models': _separate_models_evaluations,
            'evaluations': _separate_modules_evaluations
        }

        separator = mode_func.get(mode)
        if not separator:
            return
        separator(config)

    @staticmethod
    def _previous_configuration_parameters_sharing(config, mode='models'):
        def _share_params_models(models_config):
            shared_params = {parameter: None for parameter in CONFIG_SHARED_PARAMETERS}
            for model in models_config['models']:
                launchers = model['launchers']
                if not launchers:
                    continue
                for launcher in model['launchers']:
                    for parameter in CONFIG_SHARED_PARAMETERS:
                        if parameter in launcher:
                            if shared_params[parameter] is not None:
                                launcher['_prev_{}'.format(parameter)] = shared_params[parameter]
                            shared_params[parameter] = launcher[parameter]

        def _share_params_modules(modules_config):
            shared_params = {parameter: None for parameter in CONFIG_SHARED_PARAMETERS}
            for evaluation in modules_config['evaluations']:
                if 'module_config' not in evaluation:
                    continue
                launchers = evaluation['module_config'].get('launchers')
                for launcher in launchers:
                    for parameter in CONFIG_SHARED_PARAMETERS:
                        if parameter in launcher:
                            if shared_params[parameter] is not None:
                                launcher['_prev_{}'.format(parameter)] = shared_params[parameter]
                            shared_params[parameter] = launcher[parameter]

        mode_func = {
            'models': _share_params_models,
            'evaluations': _share_params_modules,
        }

        processor = mode_func.get(mode)
        if not processor:
            return
        processor(config)

    @staticmethod
    def convert_paths(config):
        mode = 'evaluations' if 'evaluations' in config else 'models'
        definitions = os.environ.get(DEFINITION_ENV_VAR)
        args = {}
        if definitions:
            definitions = read_yaml(Path(definitions))
            ConfigReader._prepare_global_configs(definitions)
            config = ConfigReader._merge_configs(definitions, config, {}, mode)
        ConfigReader._merge_paths_with_prefixes(args, config, mode)
        if COMMAND_LINE_ARGS_AS_ENV_VARS['kaldi_bin_dir'] in os.environ:
            ConfigReader._provide_cmd_arguments(Namespace(**args), config, mode)

        def convert_launcher_paths(launcher_config):
            for key, path in launcher_config.items():
                if key not in ENTRIES_PATHS['launchers']:
                    continue
                launcher_config[key] = Path(path)
            adapter_config = launcher_config.get('adapter')
            if isinstance(adapter_config, dict):
                command_line_adapter = (create_command_line_mapping(adapter_config, None))
                for arg in command_line_adapter:
                    adapter_config[arg] = Path(adapter_config[arg])

        def convert_dataset_paths(dataset_config):
            conversion_config = dataset_config.get('annotation_conversion')
            if conversion_config:
                command_line_conversion = (create_command_line_mapping(conversion_config, None))
                for conversion_path in command_line_conversion:
                    conversion_config[conversion_path] = Path(conversion_config[conversion_path])

            if 'preprocessing' in dataset_config:
                for preprocessor in dataset_config['preprocessing']:
                    path_preprocessing = (create_command_line_mapping(preprocessor, None))
                    for path in path_preprocessing:
                        preprocessor[path] = Path(preprocessor[path])

            for key, path in dataset_config.items():
                if key not in ENTRIES_PATHS['datasets']:
                    continue
                dataset_config[key] = Path(path)

        if mode == 'models':
            for model in config['models']:
                for launcher_config in model['launchers']:
                    convert_launcher_paths(launcher_config)
                datasets = model['datasets'].values() if isinstance(model['datasets'], dict) else model['datasets']
                for dataset_config in datasets:
                    convert_dataset_paths(dataset_config)
        else:
            for evaluation in config['evaluations']:
                module_config = evaluation.get('module_config', {})
                for launcher_config in module_config.get('launchers', []):
                    convert_launcher_paths(launcher_config)
                d_config = module_config.get('datasets')
                if d_config:
                    datasets = d_config.values() if isinstance(d_config, dict) else d_config
                    for dataset_config in datasets:
                        convert_dataset_paths(dataset_config)

        return config


def create_command_line_mapping(config, default_value, value_map=None):
    mapping = {}
    value_map = value_map or {}
    for key, value in config.items():
        if key.endswith('file') or key.endswith('dir'):
            if not Path(value).is_absolute():
                mapping[key] = value_map.get(key, default_value)

    return mapping


def filtered(launcher, targets, args):
    target_tags = args.get('target_tags') or []
    target_backends = args.get('target_backends')
    use_new_api = args.get('use_new_api', False)
    target_framework = args.get('target_framework', '')
    if target_framework and target_framework == 'dlsdk' and use_new_api:
        target_framework = 'openvino'
    if target_tags:
        if not contains_any(target_tags, launcher.get('tags', [])):
            return True

    config_framework = launcher['framework'].lower()
    if not target_framework:
        target_framework = config_framework
    target_framework = target_framework.lower()
    if config_framework != target_framework:
        return True

    if target_backends:
        backend = launcher.get('backend')
        if backend not in target_backends:
            return True

    return targets and launcher.get('device', '').lower() not in targets


def complete_openvino_launchers(launchers, use_new_api):
    if use_new_api is None:
        return launchers
    orig_ov_launchers = []
    updated_ov_launchers = []

    for idx, launcher in enumerate(launchers):
        fwk = launcher.get('framework')
        if fwk not in ['openvino', 'dlsdk']:
            continue
        if use_new_api:
            if fwk == 'openvino':
                orig_ov_launchers.append(idx)
            else:
                updated_ov_launchers.append(idx)
        else:
            if fwk == 'openvino':
                updated_ov_launchers.append(idx)
            else:
                orig_ov_launchers.append(idx)
    if not orig_ov_launchers:
        for idx in updated_ov_launchers:
            launchers[idx]['framework'] = 'openvino' if use_new_api else 'dlsdk'
    return launchers


def filter_models(config, target_devices, args):
    models_after_filtration = []
    for model in config['models']:
        launchers_after_filtration = []
        launchers = model['launchers']
        if 'use_new_api' in args:
            launchers = complete_openvino_launchers(launchers, args['use_new_api'])
        for launcher in launchers:
            if 'device' not in launcher and target_devices:
                for device in target_devices:
                    launcher_with_device = copy.deepcopy(launcher)
                    launcher_with_device['device'] = device
                    if not filtered(launcher_with_device, target_devices, args):
                        launchers_after_filtration.append(launcher_with_device)
                continue
            if not filtered(launcher, target_devices, args):
                launchers_after_filtration.append(launcher)

        if not launchers_after_filtration:
            warnings.warn('Model "{}" has no launchers'.format(model['name']))
            continue

        model['launchers'] = launchers_after_filtration
        models_after_filtration.append(model)

    config['models'] = models_after_filtration


def filter_modules(config, target_devices, args):
    filtered_evals = []
    for evaluation in config['evaluations']:
        if 'module_config' not in evaluation or 'launchers' not in evaluation['module_config']:
            if target_devices:
                warnings.warn(
                    'Information about launcher is not provided in config for {}. '
                    'Filtration can not be done'.format(evaluation['name'])
                )
            filtered_evals.append(evaluation)
            continue
        module_config = evaluation['module_config']
        launchers = module_config['launchers']
        if 'use_new_api' in args:
            launchers = complete_openvino_launchers(launchers, args['use_new_api'])
        if target_devices:
            launchers_without_device = [launcher for launcher in launchers if 'device' not in launcher]
            for launcher in launchers_without_device:
                for device in target_devices:
                    launcher_with_device = copy.deepcopy(launcher)
                    launcher_with_device['device'] = device
                    launchers.append(launcher_with_device)
        launchers = [
            launcher for launcher in launchers if not filtered(launcher, target_devices, args)
        ]
        if not launchers:
            warnings.warn('Model "{}" has no launchers'.format(evaluation['name']))
        evaluation['module_config']['launchers'] = launchers
        filtered_evals.append(evaluation)
    config['evaluations'] = filtered_evals


def process_config(
    config_item, entries_paths, args, dataset_identifier='datasets',
    launchers_identifier='launchers', identifiers_mapping=None, pipeline=False
):
    def process_dataset(datasets_configs):
        for datasets_config in datasets_configs:
            annotation_conversion_config = datasets_config.get('annotation_conversion')
            if annotation_conversion_config:
                command_line_conversion = (create_command_line_mapping(annotation_conversion_config,
                                                                       'source', ANNOTATION_CONVERSION_PATHS))
                datasets_config['_command_line_mapping'] = prepare_commandline_conversion_mapping(
                    command_line_conversion, args
                )
                merge_entry_paths(command_line_conversion, annotation_conversion_config, args)
            if 'preprocessing' in datasets_config:
                for preprocessor in datasets_config['preprocessing']:
                    merge_entry_paths(create_command_line_mapping(preprocessor, 'models', PREPROCESSING_PATHS),
                                      preprocessor, args)

    def process_launchers(launchers_configs):
        if not isinstance(launchers_configs, list):
            launchers_configs = [launchers_configs]

        updated_launchers = []
        for launcher_config in launchers_configs:
            if ('models' not in args or not args['models']) and not isinstance(launcher_config.get('adapter'), dict):
                updated_launchers.append(launcher_config)
                continue
            models = args.get('models')
            if isinstance(models, list):
                for model_id, _ in enumerate(models):
                    new_launcher = copy.deepcopy(launcher_config)
                    merge_entry_paths(LIST_ENTRIES_PATHS, new_launcher, args, model_id)
                    adapter_config = new_launcher.get('adapter')
                    if isinstance(adapter_config, dict):
                        command_line_adapter = (create_command_line_mapping(adapter_config, 'models', ADAPTERS_PATHS))
                        merge_entry_paths(command_line_adapter, adapter_config, args, model_id)
                    if not updated_launchers or new_launcher != updated_launchers[-1]:
                        updated_launchers.append(new_launcher)
            else:
                merge_entry_paths(LIST_ENTRIES_PATHS, launcher_config, args)
                adapter_config = launcher_config.get('adapter')
                if isinstance(adapter_config, dict):
                    command_line_adapter = (create_command_line_mapping(adapter_config, 'models', ADAPTERS_PATHS))
                    merge_entry_paths(command_line_adapter, adapter_config, args)
                updated_launchers.append(launcher_config)

        return updated_launchers

    for entry, command_line_arg in entries_paths.items():
        entry_id = entry if not identifiers_mapping else identifiers_mapping[entry]
        if entry_id not in config_item:
            continue

        if entry_id == dataset_identifier:
            datasets_config = config_item[entry_id]
            dataset_processing_config = (
                list(datasets_config.values()) if isinstance(datasets_config, dict) and not pipeline
                else datasets_config
            )
            if not isinstance(dataset_processing_config, list):
                dataset_processing_config = [dataset_processing_config]
            process_dataset(dataset_processing_config)
            for config_entry in dataset_processing_config:
                merge_entry_paths(command_line_arg, config_entry, args)
            continue

        if entry_id == launchers_identifier:
            launchers_configs = config_item[entry_id]
            processed_launcher = process_launchers(launchers_configs)
            config_item[entry_id] = processed_launcher if not pipeline else processed_launcher[0]

        config_entries = config_item[entry_id]
        if not isinstance(config_entries, list):
            config_entries = [config_entries]
        for config_entry in config_entries:
            merge_entry_paths(command_line_arg, config_entry, args)


def select_arg_path(selected_argument, value_id, argument):
    if isinstance(selected_argument, list):
        if len(selected_argument) > 1:
            if len(selected_argument) <= value_id:
                raise ValueError('list of arguments for {} less than number of evaluations'.format(argument))
            selected_argument = selected_argument[value_id]
        else:
            selected_argument = selected_argument[0]
    if not isinstance(selected_argument, Path):
        selected_argument = Path(selected_argument)
    return selected_argument


def merge_entry_paths(keys, value, args, value_id=0):
    for field, argument in keys.items():
        if not is_iterable(value) or field not in value:
            continue

        config_path = Path(value[field])
        if config_path.is_absolute():
            value[field] = Path(value[field])
            continue
        argument_list = argument

        if not isinstance(argument, list):
            argument_list = [argument]

        selected_argument = None
        for arg_candidate in argument_list:

            if arg_candidate not in args or not args[arg_candidate]:
                continue

            selected_argument = select_arg_path(args[arg_candidate], value_id, argument)
            prefix_path = selected_argument
            if not selected_argument.is_dir():
                if arg_candidate in ALLOW_FILE_OR_DIR:
                    prefix_path = selected_argument.parent
                else:
                    raise ConfigError('argument: {} should be a directory'.format(argument))

            if (prefix_path / config_path).exists():
                break
        value[field] = selected_argument / config_path if selected_argument is not None else config_path


def get_mode(config):
    evaluation_keys = [key for key in config if key != 'global_definitions']
    if not evaluation_keys:
        raise ConfigError('Invalid config structure. No evaluations detected.')
    if len(evaluation_keys) > 1:
        raise ConfigError('Multiple evaluation types in the one config is not supported. '
                          'Please separate on several configs.')
    return next(iter(evaluation_keys))


def merge_dlsdk_launcher_args(arguments, launcher_entry, update_launcher_entry):
    def _async_evaluation_args(launcher_entry):
        if 'async_mode' in arguments:
            launcher_entry['async_mode'] = arguments.async_mode

        if 'num_requests' in arguments and arguments.num_requests is not None:
            launcher_entry['num_requests'] = arguments.num_requests

        return launcher_entry

    kaldi_binaries = arguments.kaldi_bin_dir if 'kaldi_bin_dir' in arguments else None
    kaldi_logs = arguments.kaldi_log_file if 'kaldi_log_file' in arguments else None
    precision_hint = arguments.inference_precision_hint if 'inference_precision_hint' in arguments else None
    if kaldi_binaries:
        launcher_entry['_kaldi_bin_dir'] = kaldi_binaries
        launcher_entry['_kaldi_log_file'] = kaldi_logs
    if precision_hint:
        launcher_entry['_inference_precision_hint'] = precision_hint
    if launcher_entry['framework'].lower() not in ['dlsdk', 'openvino']:
        return launcher_entry

    launcher_entry.update(update_launcher_entry)
    _async_evaluation_args(launcher_entry)

    if 'device_config' in arguments and arguments.device_config:
        merge_device_configs(launcher_entry, arguments.device_config)

    if 'cpu_extensions' not in launcher_entry and 'extensions' in arguments and arguments.extensions:
        extensions = arguments.extensions
        if not extensions.is_dir() or extensions.name == 'AUTO':
            launcher_entry['cpu_extensions'] = arguments.extensions

    if 'affinity_map' not in launcher_entry and 'affinity_map' in arguments and arguments.affinity_map:
        am = arguments.affinity_map
        if not am.is_dir():
            launcher_entry['affinity_map'] = arguments.affinity_map

    if 'undefined_shapes_resolving_policy' in arguments:
        launcher_entry['_undefined_shapes_resolving_policy'] = arguments.undefined_shapes_resolving_policy

    return launcher_entry


def _add_subset_specific_arg(dataset_entry, arguments):
    if 'shuffle' in arguments and arguments.shuffle is not None:
        dataset_entry['shuffle'] = arguments.shuffle

    if 'subsample_size' in arguments and arguments.subsample_size is not None:
        dataset_entry['subsample_size'] = arguments.subsample_size
    if 'subset_file' in arguments and arguments.subset_file is not None:
        dataset_entry['subset_file'] = arguments.subset_file
    if 'store_subset' in arguments and arguments.store_subset:
        dataset_entry['store_subset'] = arguments.store_subset


def prepare_commandline_conversion_mapping(commandline_conversion, args):
    mapping = {}
    for key, value in commandline_conversion.items():
        if not isinstance(value, list):
            mapping[key] = args.get(value)
        else:
            possible_paths = []
            for v in value:
                if args.get(v) is None:
                    continue
                possible_paths.append(args[v])
            mapping[key] = possible_paths

    return mapping


def merge_device_configs(launcher_entry, device_config_file):
    embedded_device_config = launcher_entry.get('device_config')
    external_device_config = read_yaml(device_config_file)
    if not embedded_device_config:
        embedded_device_config = external_device_config
    elif (
        not isinstance(next(iter(external_device_config.values())), dict)
        and not isinstance(next(iter(embedded_device_config.values())), dict)
    ):
        embedded_device_config.update(external_device_config)
    else:
        for key, value in external_device_config.items():
            if key not in embedded_device_config:
                embedded_device_config[key] = {}
            embedded_device_config[key].update(value)
    launcher_entry['device_config'] = embedded_device_config
    return launcher_entry


def provide_precision_and_layout(launchers, input_precisions, input_layouts):
    for launcher in launchers:
        if input_precisions:
            launcher['_input_precision'] = input_precisions
        if input_layouts:
            launcher['_input_layout'] = input_layouts


def provide_model_type(launcher, arguments):
    if 'model_type' in arguments:
        launcher['_model_type'] = arguments.model_type
    if launcher['framework'] in ['dlsdk', 'openvino', 'g-api'] and 'model_is_blob' in arguments:
        launcher['_model_is_blob'] = arguments.model_is_blob
        if arguments.model_is_blob:
            launcher['_model_type'] = 'blob'


def provide_models(launchers, arguments):
    input_precisions = arguments.input_precision if 'input_precision' in arguments else None
    input_layout = arguments.layout if 'layout' in arguments else None

    provide_precision_and_layout(launchers, input_precisions, input_layout)
    if 'models' not in arguments or not arguments.models:
        return launchers
    model_paths = arguments.models
    updated_launchers = []
    model_paths = [model_paths] if not isinstance(model_paths, list) else model_paths
    for launcher in launchers:
        if contains_any(launcher, ACCEPTABLE_MODEL):
            updated_launchers.append(launcher)
            continue
        for model_path in model_paths:
            copy_launcher = copy.deepcopy(launcher)
            copy_launcher['model'] = model_path
            provide_model_type(copy_launcher, arguments)
            updated_launchers.append(copy_launcher)
    return updated_launchers
