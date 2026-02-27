"""Runtime API Spec Obfuscation for testing code mode patterns."""

import json
import re
import tempfile
from pathlib import Path


def _obfuscate_spec(spec: dict, path_mapping: dict, tag_mapping: dict) -> dict:
    """Obfuscate an OpenAPI spec by renaming paths, tags, and operationIds.
    
    Args:
        spec: Original OpenAPI spec
        path_mapping: Dict mapping original path segments to obfuscated ones
        tag_mapping: Dict mapping original tag prefixes to obfuscated ones
        
    Returns:
        Obfuscated spec with same structure but different names
    """
    obfuscated = json.loads(json.dumps(spec))  # Deep copy
    
    # Obfuscate paths
    new_paths = {}
    for path, methods in obfuscated.get('paths', {}).items():
        # Replace path segments
        new_path = path
        for old, new in path_mapping.items():
            new_path = re.sub(f'/{old}/', f'/{new}/', new_path)
            new_path = re.sub(f'/{old}$', f'/{new}', new_path)
        
        # Obfuscate methods
        new_methods = {}
        for method, op in methods.items():
            if method not in {'get', 'post', 'put', 'delete', 'patch', 'head', 'options'}:
                new_methods[method] = op
                continue
            
            # Obfuscate tags
            if 'tags' in op:
                new_tags = []
                for tag in op['tags']:
                    new_tag = tag
                    for old, new in tag_mapping.items():
                        new_tag = new_tag.replace(old, new)
                    new_tags.append(new_tag)
                op['tags'] = new_tags
            
            # Obfuscate operationId
            if 'operationId' in op:
                op_id = op['operationId']
                for old, new in path_mapping.items():
                    # Handle camelCase (e.g., listOrgDevices → listEntityNodes)
                    old_camel = old.capitalize()
                    new_camel = new.capitalize()
                    op_id = op_id.replace(old_camel, new_camel)
                    op_id = op_id.replace(old, new)
                op['operationId'] = op_id
            
            new_methods[method] = op
        
        new_paths[new_path] = new_methods
    
    obfuscated['paths'] = new_paths
    
    # Obfuscate tags metadata
    if 'tags' in obfuscated:
        new_tag_list = []
        for tag in obfuscated['tags']:
            new_tag = tag.copy()
            name = tag.get('name', '')
            for old, new in tag_mapping.items():
                name = name.replace(old, new)
            new_tag['name'] = name
            new_tag_list.append(new_tag)
        obfuscated['tags'] = new_tag_list
    
    # Update API title
    if 'info' in obfuscated:
        obfuscated['info']['title'] = 'Obfuscated Test API'
    
    return obfuscated


def obfuscate_spec_file(input_path: Path) -> Path:
    """Read an OpenAPI spec file, obfuscate it, and save to a temporary file.

    Args:
        input_path: Path to the original OpenAPI spec file.

    Returns:
        Path to the newly created obfuscated temporary JSON spec file.
    """
    with open(input_path, 'r') as f:
        spec = json.load(f)
    
    path_mapping = {
        'orgs': 'entities',
        'sites': 'locations',
        'devices': 'nodes',
        'wlans': 'wireless_networks',
        'clients': 'endpoints',
        'self': 'current_user',
        'admins': 'administrators',
        'msps': 'service_providers',
        'const': 'constants',
        'stats': 'metrics',
    }
    
    tag_mapping = {
        'Orgs': 'Entities',
        'Sites': 'Locations',
        'Devices': 'Nodes',
        'WLANs': 'Wireless Networks',
        'Clients': 'Endpoints',
        'Self': 'Current User',
        'Admins': 'Administrators',
        'MSPs': 'Service Providers',
    }
    
    obfuscated = _obfuscate_spec(spec, path_mapping, tag_mapping)

    temp_file = tempfile.NamedTemporaryFile(
        mode='w',
        suffix='.json',
        delete=False
    )
    json.dump(obfuscated, temp_file, indent=2)
    temp_file.close()

    return Path(temp_file.name)
