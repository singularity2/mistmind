"""MCP server implementation with search and execute tools."""

import json
import logging
import secrets
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import ServerConfig
from .sandbox import DenoSandbox
from .spec_indexer import generate_index_from_file

logger = logging.getLogger(__name__)


class MistMindServer:
    """MistMind MCP server with Code Mode pattern."""

    def __init__(self, config: ServerConfig, spec_path: str):
        """Initialize server with config and resolved spec path."""
        self.config = config
        self.spec_path = Path(spec_path)
        self.sandbox = DenoSandbox(
            deno_path=config.deno_path,
            timeout=30,
            api_mode=config.mistmind_api_mode,
            rate_limit=config.mistmind_rate_limit,
            max_concurrent=config.mistmind_max_concurrent,
        )
        self.server = Server("mistmind")
        self.private_test_mode = config.mistmind_private_test_mode
        self.search_token_ttl_seconds = max(30, config.mistmind_search_token_ttl_seconds)
        self._search_sessions: dict[str, dict[str, Any]] = {}
        self._real_to_alias_path: dict[str, str] = {}
        self._alias_to_real_path: dict[str, str] = {}
        
        # Verify spec exists
        if not self.spec_path.exists():
            raise FileNotFoundError(
                f"Resolved spec not found at {self.spec_path}. "
                f"Please run: python -m mistmind.spec_resolver "
                f"spec/mist.openapi.json spec/mist.resolved.json"
            )
        
        # Generate dynamic index from spec
        logger.info("Generating spec index...")
        self.spec_index = generate_index_from_file(str(self.spec_path))
        logger.info(f"Spec index generated (~{len(self.spec_index) // 4} tokens)")

        if self.private_test_mode:
            self._real_to_alias_path, self._alias_to_real_path = self._build_obfuscated_path_maps()
            logger.info(
                "Strict private test mode enabled: %s obfuscated paths loaded, token ttl=%ss",
                len(self._alias_to_real_path),
                self.search_token_ttl_seconds,
            )
        
        self._register_handlers()

    def _obfuscate_path_template(self, path_template: str) -> str:
        """Generate deterministic, non-semantic path aliases for strict private mode."""
        segments = path_template.strip("/").split("/")
        if not segments or segments == [""]:
            return "/"

        obfuscated: list[str] = []
        for idx, segment in enumerate(segments):
            if segment.startswith("{") and segment.endswith("}"):
                obfuscated.append(segment)
                continue
            digest = sha256(f"{path_template}:{idx}:{segment}".encode("utf-8")).hexdigest()[:10]
            obfuscated.append(f"s_{digest}")

        return "/" + "/".join(obfuscated)

    def _build_obfuscated_path_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """Build deterministic real<->obfuscated path mappings from the local spec."""
        spec_data = json.loads(self.spec_path.read_text(encoding="utf-8"))
        paths = spec_data.get("paths", {})

        real_to_alias: dict[str, str] = {}
        alias_to_real: dict[str, str] = {}

        for real_path in paths:
            alias_path = self._obfuscate_path_template(real_path)
            if alias_path in alias_to_real and alias_to_real[alias_path] != real_path:
                suffix = 1
                candidate = f"{alias_path}__{suffix}"
                while candidate in alias_to_real and alias_to_real[candidate] != real_path:
                    suffix += 1
                    candidate = f"{alias_path}__{suffix}"
                alias_path = candidate

            real_to_alias[real_path] = alias_path
            alias_to_real[alias_path] = real_path

        return real_to_alias, alias_to_real

    def _cleanup_expired_sessions(self):
        """Drop expired search sessions."""
        now = time.time()
        expired = [token for token, session in self._search_sessions.items() if session["expires_at"] <= now]
        for token in expired:
            self._search_sessions.pop(token, None)

    def _create_search_session(self) -> tuple[str, int]:
        """Create a search approval session and return token + ttl."""
        self._cleanup_expired_sessions()
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + self.search_token_ttl_seconds
        self._search_sessions[token] = {
            "expires_at": expires_at,
            "alias_to_real_path": dict(self._alias_to_real_path),
        }
        return token, self.search_token_ttl_seconds

    def _get_search_session(self, token: str) -> dict[str, Any] | None:
        """Return a valid non-expired search session."""
        self._cleanup_expired_sessions()
        session = self._search_sessions.get(token)
        if not session:
            return None
        if session["expires_at"] <= time.time():
            self._search_sessions.pop(token, None)
            return None
        return session

    def _register_handlers(self):
        """Register MCP tool handlers."""
        
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available tools."""
            search_description = self.spec_index
            execute_description = (
                "Execute JS against the Mist API. Use mist.request({method, path, body, params}).\n"
                "method defaults to GET. Chain multiple calls, filter/transform results in JS.\n"
                "mist.allowedMethods shows permitted HTTP methods.\n"
                "For paginated results: check if total > results.length, loop with page/start params.\n"
                "For write ops: return a preview first, execute write only after user confirms."
            )
            execute_code_description = (
                "JavaScript async arrow function to execute. "
                "Example: async () => { const self = await mist.request({path: '/api/v1/self'}); return self; }"
            )
            execute_required = ["code"]
            execute_properties: dict[str, Any] = {
                "code": {
                    "type": "string",
                    "description": execute_code_description,
                }
            }

            if self.private_test_mode:
                search_description += (
                    "\n\nSTRICT PRIVATE TEST MODE: spec.paths are obfuscated aliases. "
                    "Search responses include search_token. Pass it to execute."
                )
                execute_description += (
                    "\nStrict private test mode is enabled: paths must use obfuscated templates from search, "
                    "and search_token is required."
                )
                execute_properties["code"]["description"] = (
                    "JavaScript async arrow function using obfuscated path templates from search output."
                )
                execute_properties["search_token"] = {
                    "type": "string",
                    "description": "Token returned by the latest search call.",
                }
                execute_required.append("search_token")

            return [
                Tool(
                    name="search",
                    description=search_description,
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": (
                                    "JavaScript async arrow function to search the OpenAPI spec. "
                                    "Example: async () => { const results = []; for (const [path, methods] "
                                    "of Object.entries(spec.paths)) { for (const [method, op] of "
                                    "Object.entries(methods)) { if (op.tags?.some(t => "
                                    't.toLowerCase().includes("wireless"))) results.push({method: '
                                    "method.toUpperCase(), path, summary: op.summary}); } } return results; }"
                                ),
                            }
                        },
                        "required": ["code"],
                    },
                ),
                Tool(
                    name="execute",
                    description=execute_description,
                    inputSchema={
                        "type": "object",
                        "properties": execute_properties,
                        "required": execute_required,
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Any) -> list[TextContent]:
            """Handle tool calls."""
            try:
                if name == "search":
                    return await self._handle_search(arguments)
                elif name == "execute":
                    return await self._handle_execute(arguments)
                else:
                    return [
                        TextContent(
                            type="text",
                            text=f"Unknown tool: {name}",
                        )
                    ]
            except Exception as e:
                logger.error(f"Tool call error: {e}", exc_info=True)
                error_msg = str(e)
                # BUG 3 FIX: Scrub token from exception messages
                if hasattr(self, 'config') and self.config.mist_apitoken:
                    error_msg = error_msg.replace(self.config.mist_apitoken, "[REDACTED]")
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {error_msg}",
                    )
                ]

    async def _handle_search(self, arguments: dict) -> list[TextContent]:
        """Handle search tool call."""
        code = arguments.get("code")
        if not code:
            return [TextContent(type="text", text="Error: 'code' parameter required")]
        
        logger.info(f"Executing search with code length: {len(code)}")
        
        result = await self.sandbox.run_search(
            code=code,
            spec_path=str(self.spec_path),
            path_alias_map=self._real_to_alias_path if self.private_test_mode else None,
        )
        if self.private_test_mode:
            token, ttl_seconds = self._create_search_session()
            result = {
                "search_token": token,
                "expires_in_seconds": ttl_seconds,
                "result": result,
            }
        
        # Format result as text
        result_text = json.dumps(result, indent=2)
        
        return [TextContent(type="text", text=result_text)]

    async def _handle_execute(self, arguments: dict) -> list[TextContent]:
        """Handle execute tool call."""
        code = arguments.get("code")
        if not code:
            return [TextContent(type="text", text="Error: 'code' parameter required")]

        approved_alias_to_real_path: dict[str, str] | None = None
        if self.private_test_mode:
            search_token = arguments.get("search_token")
            if not search_token:
                return [
                    TextContent(
                        type="text",
                        text="Error: 'search_token' parameter required in strict private test mode. Run search first.",
                    )
                ]
            session = self._get_search_session(search_token)
            if not session:
                return [
                    TextContent(
                        type="text",
                        text="Error: invalid or expired search_token. Run search again to get a fresh token.",
                    )
                ]
            approved_alias_to_real_path = session["alias_to_real_path"]
        
        logger.info(f"Executing API call with code length: {len(code)}")
        
        result = await self.sandbox.run_execute(
            code=code,
            api_token=self.config.mist_apitoken,
            api_host=self.config.mist_host,
            approved_alias_to_real_path=approved_alias_to_real_path,
        )
        
        # Format result as text
        result_text = json.dumps(result, indent=2)
        
        return [TextContent(type="text", text=result_text)]

    async def run(self):
        """Run the MCP server."""
        logger.info("Starting MistMind MCP server...")
        logger.info(f"Spec path: {self.spec_path}")
        logger.info(f"Deno path: {self.config.deno_path}")
        logger.info(f"API host: {self.config.mist_host}")
        
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )
