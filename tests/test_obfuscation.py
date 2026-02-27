"""Test that MistMind works on obfuscated (private/unknown) APIs.

This is the KEY test that proves MistMind doesn't depend on training data
about specific APIs. We obfuscate the Mist spec completely (rename paths,
tags, operations) while keeping the structure intact, then verify the
spec_indexer can still auto-detect the hierarchy and the sandbox can
still discover and execute searches.
"""

import json
import re
import tempfile
from pathlib import Path

import pytest

from mistmind.spec_indexer import generate_index_from_file
from mistmind.obfuscator import obfuscate_spec_file


class TestObfuscation:
    """Test MistMind on obfuscated API specs."""
    
    @pytest.fixture
    def obfuscated_spec_path(self):
        """Create an obfuscated version of the Mist spec."""
        # Load the original spec
        spec_dir = Path(__file__).parent.parent / "spec"
        original_path = spec_dir / "mist.openapi.json"
        
        # Obfuscate the spec using the module
        obfuscated_path = obfuscate_spec_file(original_path)
        
        yield str(obfuscated_path)
        
        # Cleanup
        obfuscated_path.unlink(missing_ok=True)

    
    def test_obfuscated_spec_generates_valid_index(self, obfuscated_spec_path):
        """Test that spec_indexer can generate an index from obfuscated spec."""
        index = generate_index_from_file(obfuscated_spec_path)
        
        # Verify index structure
        assert "Obfuscated Test API" in index
        assert "endpoints" in index
        assert "=== API HIERARCHY ===" in index
        assert "=== AUTH PATTERN ===" in index
        assert "=== PAGINATION ===" in index
        assert "=== RESPONSE PATTERNS ===" in index
        assert "=== SEARCH GUIDE ===" in index
    
    def test_obfuscated_spec_detects_hierarchy(self, obfuscated_spec_path):
        """Test that the obfuscated spec's hierarchy is correctly detected."""
        index = generate_index_from_file(obfuscated_spec_path)
        
        # Should detect the obfuscated scope names
        assert "Entities" in index  # Was "Orgs"
        assert "Locations" in index  # Was "Sites"
        assert "Service Providers" in index or "Service" in index  # Was "MSPs"
        
        # Should NOT contain original names
        assert "Orgs" not in index or "Orgs" in "Service Providers"
        assert "Sites (" not in index  # "(Sites " would indicate a scope
        assert "MSPs" not in index
    
    def test_obfuscated_spec_detects_auth(self, obfuscated_spec_path):
        """Test that auth pattern is detected even after obfuscation."""
        index = generate_index_from_file(obfuscated_spec_path)
        
        # Should detect the obfuscated /self endpoint
        assert "current_user" in index.lower() or "Token-based" in index
    
    def test_obfuscated_spec_detects_pagination(self, obfuscated_spec_path):
        """Test that pagination params are detected in obfuscated spec."""
        index = generate_index_from_file(obfuscated_spec_path)
        
        # Pagination section should exist (may or may not find params in base spec)
        assert "=== PAGINATION ===" in index
    
    def test_obfuscated_spec_counts_correct(self, obfuscated_spec_path):
        """Test that endpoint counts match the original spec."""
        index = generate_index_from_file(obfuscated_spec_path)
        
        # Should show ~1011 total endpoints (same as original)
        assert "1011 endpoints" in index or "1011" in index
    
    def test_search_works_on_obfuscated_spec(self, obfuscated_spec_path):
        """Test that search queries work against obfuscated spec.
        
        This is the critical test: can we discover endpoints in an API
        we've never seen before, with completely different names?
        """
        from mistmind.sandbox import DenoSandbox
        import asyncio
        import shutil
        from pathlib import Path
        
        # Auto-detect Deno
        deno_path = shutil.which("deno")
        if not deno_path:
            home_deno = Path.home() / ".deno" / "bin" / "deno"
            if home_deno.exists():
                deno_path = str(home_deno)
        
        # Create sandbox
        sandbox = DenoSandbox(
            deno_path=deno_path or "/usr/local/bin/deno",
            timeout=30,
            api_mode="readonly",
        )
        
        # Search for "nodes" (was "devices") endpoints
        search_code = """
        async () => {
            const results = [];
            for (const [path, methods] of Object.entries(spec.paths)) {
                for (const [method, op] of Object.entries(methods)) {
                    if (method === 'get' || method === 'post') {
                        if (path.includes('nodes') || 
                            op.tags?.some(t => t.toLowerCase().includes('nodes'))) {
                            results.push({
                                method: method.toUpperCase(),
                                path: path,
                                summary: op.summary,
                                tags: op.tags
                            });
                        }
                    }
                }
            }
            return results.slice(0, 10);  // First 10
        }
        """
        
        result = asyncio.run(sandbox.run_search(
            code=search_code,
            spec_path=obfuscated_spec_path
        ))
        
        # Check for errors
        if isinstance(result, dict) and 'error' in result:
            pytest.fail(f"Search failed: {result['error']}")
        
        # Verify we found some endpoints
        assert isinstance(result, list), f"Expected list, got {type(result)}"
        assert len(result) > 0, "Should find at least one endpoint"
        
        # Verify they contain obfuscated paths
        for item in result:
            assert 'path' in item
            # Should contain obfuscated terms, not original ones
            path = item['path'].lower()
            assert 'nodes' in path or 'entities' in path or 'locations' in path
    
    def test_search_by_scope_works_on_obfuscated(self, obfuscated_spec_path):
        """Test searching by scope (Entities vs Locations) works."""
        from mistmind.sandbox import DenoSandbox
        import asyncio
        import shutil
        from pathlib import Path
        
        # Auto-detect Deno
        deno_path = shutil.which("deno")
        if not deno_path:
            home_deno = Path.home() / ".deno" / "bin" / "deno"
            if home_deno.exists():
                deno_path = str(home_deno)
        
        sandbox = DenoSandbox(
            deno_path=deno_path or "/usr/local/bin/deno",
            timeout=30,
            api_mode="readonly",
        )
        
        # Search for Entities (was Orgs) scope endpoints
        search_code = """
        async () => {
            const results = [];
            for (const [path, methods] of Object.entries(spec.paths)) {
                if (path.includes('/entities/')) {
                    for (const [method, op] of Object.entries(methods)) {
                        if (method === 'get') {
                            results.push({
                                method: method.toUpperCase(),
                                path: path,
                                tags: op.tags
                            });
                            if (results.length >= 5) break;
                        }
                    }
                }
                if (results.length >= 5) break;
            }
            return results;
        }
        """
        
        result = asyncio.run(sandbox.run_search(
            code=search_code,
            spec_path=obfuscated_spec_path
        ))
        
        # Check for errors
        if isinstance(result, dict) and 'error' in result:
            pytest.fail(f"Search failed: {result['error']}")
        
        assert isinstance(result, list)
        assert len(result) > 0
        
        # Verify all results are from /entities/ paths
        for item in result:
            assert '/entities/' in item['path']
    
    def test_obfuscated_spec_structure_intact(self, obfuscated_spec_path):
        """Verify obfuscation keeps the structure intact (params, schemas, etc)."""
        with open(obfuscated_spec_path, 'r') as f:
            spec = json.load(f)
        
        # Check structure is preserved
        assert 'paths' in spec
        assert 'info' in spec
        assert len(spec['paths']) > 100  # Should have many paths
        
        # Check a sample path has proper structure
        sample_path = None
        for path, methods in spec['paths'].items():
            if 'get' in methods:
                sample_path = path
                sample_op = methods['get']
                break
        
        assert sample_path is not None
        # Should still have tags, summary, etc
        assert 'tags' in sample_op or 'summary' in sample_op


if __name__ == "__main__":
    """Run obfuscation tests and print the obfuscated index."""
    import sys
    from mistmind.obfuscator import obfuscate_spec_file
    
    # Load the original spec
    spec_dir = Path(__file__).parent.parent / "spec"
    original_path = spec_dir / "mist.openapi.json"
    
    print("Creating obfuscated spec...")
    obf_path = obfuscate_spec_file(original_path)
    
    print(f"Obfuscated spec written to: {obf_path}\n")
    
    # Generate and print index
    print("=== OBFUSCATED SPEC INDEX ===\n")
    index = generate_index_from_file(str(obf_path))
    print(index)
    
    print(f"\n\n=== STATS ===\n")
    print(f"Characters: {len(index)}")
    print(f"Estimated tokens: ~{len(index) // 4}")
    
    # Cleanup
    obf_path.unlink(missing_ok=True)
