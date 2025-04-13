"""
Provides the AgentVaultClient for interacting with remote agents via the A2A protocol.

Note: Assumes a baseline A2A protocol version inspired by early drafts.
Future versions will need more robust version handling based on AgentCard capabilities.
"""

import asyncio
import json
import logging
import httpx
import typing
import uuid
import pydantic
# --- ADDED: Import time ---
import time
# --- END ADDED ---
from typing import Optional, Dict, Any, Union, AsyncGenerator, Tuple

# Import local models
from agentvault.models.agent_card import AgentCard, AgentAuthentication
from agentvault.models.a2a_protocol import (
    Message, Task, TaskState, TaskStatusUpdateEvent, TaskArtifactUpdateEvent,
    TaskMessageEvent, TaskSendParams, TaskSendResult, TaskGetParams,
    GetTaskResult, TaskCancelParams, TaskCancelResult
)
# Import local exceptions
from agentvault.exceptions import (
    AgentVaultError, A2AError, A2AConnectionError, A2AAuthenticationError,
    A2ARemoteAgentError, A2ATimeoutError, A2AMessageError, KeyManagementError
)
# Import KeyManager
from agentvault.key_manager import KeyManager
# Import MCP Utils
from agentvault.mcp_utils import format_mcp_context


logger = logging.getLogger(__name__)

A2AEvent = Union[TaskStatusUpdateEvent, TaskMessageEvent, TaskArtifactUpdateEvent]

SSE_EVENT_TYPE_MAP = {
    "task_status": TaskStatusUpdateEvent,
    "task_message": TaskMessageEvent,
    "task_artifact": TaskArtifactUpdateEvent,
    "message": TaskMessageEvent, # Allow 'message' as an alias for task_message
}

# --- ADDED: Cache constants ---
CACHE_EXPIRY_BUFFER_SECONDS = 60 # Fetch token 60s before it actually expires
# --- END ADDED ---

class AgentVaultClient:
    """
    Client for interacting with remote agents using the Agent-to-Agent (A2A) protocol.
    Handles establishing connections, sending/receiving messages, managing task lifecycles,
    and basic authentication based on Agent Card information.
    """
    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        default_timeout: float = 30.0
    ):
        """Initializes the AgentVaultClient."""
        self.default_timeout = default_timeout
        if http_client:
            self._http_client = http_client
            self._should_close_client = False
            logger.debug("Using provided httpx.AsyncClient instance.")
        else:
            logger.debug(f"Creating internal httpx.AsyncClient instance with timeout {default_timeout}s.")
            self._http_client = httpx.AsyncClient(
                timeout=default_timeout,
                http2=True,
                follow_redirects=True
            )
            self._should_close_client = True
        # Simple in-memory token cache
        self._token_cache: Dict[str, Tuple[str, Optional[float]]] = {} # service_id -> (token, expiry_timestamp)


    async def close(self) -> None:
        """Closes the underlying HTTP client if it was created internally."""
        if self._should_close_client and not self._http_client.is_closed:
            logger.debug("Closing internally managed httpx.AsyncClient instance.")
            await self._http_client.aclose()
        elif not self._should_close_client:
             logger.debug("Using externally managed httpx.AsyncClient, not closing.")

    async def __aenter__(self) -> "AgentVaultClient":
        """Enter the async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the async context manager, ensuring the client is closed."""
        await self.close()

    # --- Public A2A Methods ---

    async def initiate_task(
        self, agent_card: AgentCard, initial_message: Message, key_manager: KeyManager,
        mcp_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Initiates a new task with the remote agent by sending the first message.
        """
        logger.info(f"Initiating task with agent: {agent_card.human_readable_id}")
        try:
            auth_headers = await self._get_auth_headers(agent_card, key_manager)
            message_to_send = initial_message
            if mcp_context:
                formatted_mcp = format_mcp_context(mcp_context)
                if formatted_mcp is not None:
                    current_metadata = message_to_send.metadata or {}
                    updated_metadata = {**current_metadata, "mcp_context": formatted_mcp}
                    try: message_to_send = message_to_send.model_copy(update={'metadata': updated_metadata})
                    except AttributeError: message_to_send.metadata = updated_metadata
                    logger.debug("Successfully formatted and embedded MCP context into message metadata.")
                else: logger.warning("Failed to format provided MCP context data. Proceeding without embedding it.")

            task_send_params = TaskSendParams(message=message_to_send, id=None)
            request_id = f"req-init-{uuid.uuid4()}"
            request_params_dict = task_send_params.model_dump(mode='json', exclude_none=True, by_alias=True)
            request_payload = {"jsonrpc": "2.0", "method": "tasks/send", "params": request_params_dict, "id": request_id}
            logger.debug(f"Initiate task request payload (id: {request_id})")
            response_data = await self._make_request('POST', str(agent_card.url), headers=auth_headers, json_payload=request_payload, stream=False)

            if not isinstance(response_data, dict): raise A2AMessageError(f"Invalid response format: Expected dictionary, got {type(response_data)}")
            if "error" in response_data:
                error_data = response_data["error"]; logger.error(f"Agent returned error during task initiation: {error_data}")
                err_code = error_data.get("code", -1); err_msg = error_data.get("message", "Unknown remote agent error"); err_details = error_data.get("data")
                raise A2ARemoteAgentError(message=err_msg, status_code=err_code, response_body=err_details)
            if "result" not in response_data: raise A2AMessageError("Invalid JSON-RPC response format: missing 'result' key.")
            try: result_obj = TaskSendResult.model_validate(response_data["result"])
            except pydantic.ValidationError as e: raise A2AMessageError(f"Failed to validate task initiation result structure: {e}") from e
            task_id = result_obj.id
            if not task_id or not isinstance(task_id, str): raise A2AMessageError("Invalid response format: 'result.id' is missing, empty, or not a string.")
            logger.info(f"Task successfully initiated with agent {agent_card.human_readable_id}. Task ID: {task_id}")
            return task_id
        except (A2AAuthenticationError, A2AConnectionError, A2ARemoteAgentError, A2AMessageError, A2ATimeoutError) as e: logger.error(f"A2A error during task initiation: {type(e).__name__}: {e}"); raise
        except KeyManagementError as e: logger.error(f"Key management error during task initiation: {e}"); raise A2AAuthenticationError(f"Authentication failed due to key management error: {e}") from e
        except Exception as e: logger.exception(f"Unexpected error during task initiation with agent {agent_card.human_readable_id}: {e}"); raise A2AError(f"An unexpected error occurred during task initiation: {e}") from e

    async def send_message(
        self, agent_card: AgentCard, task_id: str, message: Message, key_manager: KeyManager,
        mcp_context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Sends a subsequent message to an existing task.
        """
        logger.info(f"Sending message to task {task_id} on agent: {agent_card.human_readable_id}")
        if not task_id or not isinstance(task_id, str): raise ValueError("Invalid task_id provided for send_message.")
        try:
            auth_headers = await self._get_auth_headers(agent_card, key_manager)
            message_to_send = message
            if mcp_context:
                formatted_mcp = format_mcp_context(mcp_context)
                if formatted_mcp is not None:
                    current_metadata = message_to_send.metadata or {}
                    updated_metadata = {**current_metadata, "mcp_context": formatted_mcp}
                    try: message_to_send = message_to_send.model_copy(update={'metadata': updated_metadata})
                    except AttributeError: message_to_send.metadata = updated_metadata
                    logger.debug("Successfully formatted and embedded MCP context into message metadata.")
                else: logger.warning("Failed to format provided MCP context data. Proceeding without embedding it.")

            task_send_params = TaskSendParams(message=message_to_send, id=task_id)
            request_id = f"req-send-{uuid.uuid4()}"
            request_params_dict = task_send_params.model_dump(mode='json', exclude_none=True, by_alias=True)
            request_payload = {"jsonrpc": "2.0", "method": "tasks/send", "params": request_params_dict, "id": request_id}
            logger.debug(f"Send message request payload (id: {request_id})")
            response_data = await self._make_request('POST', str(agent_card.url), headers=auth_headers, json_payload=request_payload, stream=False)

            if not isinstance(response_data, dict): raise A2AMessageError(f"Invalid response format: Expected dictionary, got {type(response_data)}")
            if "error" in response_data:
                error_data = response_data["error"]; logger.error(f"Agent returned error sending message to task {task_id}: {error_data}")
                err_code = error_data.get("code", -1); err_msg = error_data.get("message", "Unknown remote agent error"); err_details = error_data.get("data")
                raise A2ARemoteAgentError(message=err_msg, status_code=err_code, response_body=err_details)
            if "result" not in response_data: raise A2AMessageError("Invalid JSON-RPC response format: missing 'result' key.")
            try: TaskSendResult.model_validate(response_data["result"])
            except pydantic.ValidationError as e: raise A2AMessageError(f"Failed to validate send message result structure: {e}") from e
            logger.info(f"Message successfully sent to task {task_id} on agent {agent_card.human_readable_id}.")
            return True
        except (A2AAuthenticationError, A2AConnectionError, A2ARemoteAgentError, A2AMessageError, A2ATimeoutError) as e: logger.error(f"A2A error sending message to task {task_id}: {type(e).__name__}: {e}"); raise
        except KeyManagementError as e: logger.error(f"Key management error sending message to task {task_id}: {e}"); raise A2AAuthenticationError(f"Authentication failed due to key management error: {e}") from e
        except Exception as e: logger.exception(f"Unexpected error sending message to task {task_id} on agent {agent_card.human_readable_id}: {e}"); raise A2AError(f"An unexpected error occurred sending message: {e}") from e

    async def receive_messages(
        self, agent_card: AgentCard, task_id: str, key_manager: KeyManager
    ) -> AsyncGenerator[A2AEvent, None]:
        """
        Subscribes to Server-Sent Events (SSE) for a given task to receive messages and updates.
        """
        logger.info(f"Subscribing to events for task {task_id} on agent: {agent_card.human_readable_id}")
        if not task_id or not isinstance(task_id, str):
             raise ValueError("Invalid task_id provided for receive_messages.")

        byte_stream = None
        try:
            auth_headers = await self._get_auth_headers(agent_card, key_manager)
            auth_headers["Accept"] = "text/event-stream"

            request_id = f"req-sub-{uuid.uuid4()}"
            request_payload = {
                "jsonrpc": "2.0",
                "method": "tasks/sendSubscribe",
                "params": {"id": task_id},
                "id": request_id
            }
            logger.debug(f"Subscribe request payload (id: {request_id})")

            byte_stream_gen: AsyncGenerator[bytes, None]
            stream_result = await self._make_request(
                method='POST',
                url=str(agent_card.url),
                headers=auth_headers,
                json_payload=request_payload,
                stream=True
            )

            if not isinstance(stream_result, typing.AsyncGenerator):
                 raise A2AError(f"_make_request did not return an AsyncGenerator for stream=True (got {type(stream_result)})")
            byte_stream_gen = stream_result

            async for event_dict in self._process_sse_stream(byte_stream_gen):
                event_type = event_dict.get("event_type")
                event_data = event_dict.get("data")

                if not event_type or not isinstance(event_data, dict):
                    logger.warning(f"Skipping malformed event from SSE stream: {event_dict}")
                    continue

                event_model = SSE_EVENT_TYPE_MAP.get(event_type)
                if not event_model:
                    logger.warning(f"Received unknown SSE event type: '{event_type}'. Data: {event_data}")
                    continue

                try:
                    validated_event = event_model.model_validate(event_data)
                    logger.debug(f"Yielding validated event: {validated_event!r}")
                    yield validated_event
                except pydantic.ValidationError as e:
                    logger.error(f"Failed to validate SSE event type '{event_type}': {e}. Data: {event_data}")
                    continue

        except (A2AAuthenticationError, A2AConnectionError, A2ARemoteAgentError, A2AMessageError, A2ATimeoutError) as e:
            logger.error(f"A2A error during event subscription or processing for task {task_id}: {type(e).__name__}: {e}")
            raise
        except KeyManagementError as e:
             logger.error(f"Key management error during event subscription for task {task_id}: {e}")
             raise A2AAuthenticationError(f"Authentication failed due to key management error: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during event subscription for task {task_id} on agent {agent_card.human_readable_id}: {e}")
            raise A2AError(f"An unexpected error occurred during event subscription: {e}") from e
        finally:
             logger.debug(f"Finished receiving messages for task {task_id}.")


    async def get_task_status(
        self, agent_card: AgentCard, task_id: str, key_manager: KeyManager
    ) -> Task:
        """
        Retrieves the current status and details of a specific task.
        """
        logger.info(f"Getting status for task {task_id} on agent: {agent_card.human_readable_id}")
        if not task_id or not isinstance(task_id, str): raise ValueError("Invalid task_id provided for get_task_status.")
        try:
            auth_headers = await self._get_auth_headers(agent_card, key_manager)
            task_get_params = TaskGetParams(id=task_id)
            request_id = f"req-get-{uuid.uuid4()}"
            request_params_dict = task_get_params.model_dump(mode='json', by_alias=True)
            request_payload = {"jsonrpc": "2.0", "method": "tasks/get", "params": request_params_dict, "id": request_id}
            logger.debug(f"Get task status request payload (id: {request_id})")
            response_data = await self._make_request('POST', str(agent_card.url), headers=auth_headers, json_payload=request_payload, stream=False)

            if not isinstance(response_data, dict): raise A2AMessageError(f"Invalid response format: Expected dictionary, got {type(response_data)}")
            if "error" in response_data:
                error_data = response_data["error"]; logger.error(f"Agent returned error getting status for task {task_id}: {error_data}")
                err_code = error_data.get("code", -1); err_msg = error_data.get("message", "Unknown remote agent error"); err_details = error_data.get("data")
                raise A2ARemoteAgentError(message=err_msg, status_code=err_code, response_body=err_details)
            if "result" not in response_data: raise A2AMessageError("Invalid JSON-RPC response format: missing 'result' key.")
            try: task_object = GetTaskResult.model_validate(response_data["result"]); logger.info(f"Successfully retrieved status for task {task_id}. State: {task_object.state}"); return task_object
            except pydantic.ValidationError as e: raise A2AMessageError(f"Failed to validate task status result (Task model): {e}") from e
        except (A2AAuthenticationError, A2AConnectionError, A2ARemoteAgentError, A2AMessageError, A2ATimeoutError) as e: logger.error(f"A2A error getting status for task {task_id}: {type(e).__name__}: {e}"); raise
        except KeyManagementError as e: logger.error(f"Key management error getting status for task {task_id}: {e}"); raise A2AAuthenticationError(f"Authentication failed due to key management error: {e}") from e
        except Exception as e: logger.exception(f"Unexpected error getting status for task {task_id} on agent {agent_card.human_readable_id}: {e}"); raise A2AError(f"An unexpected error occurred getting task status: {e}") from e

    async def terminate_task(
        self, agent_card: AgentCard, task_id: str, key_manager: KeyManager
    ) -> bool:
        """
        Requests the termination (cancellation) of an ongoing task.
        """
        logger.info(f"Requesting termination for task {task_id} on agent: {agent_card.human_readable_id}")
        if not task_id or not isinstance(task_id, str): raise ValueError("Invalid task_id provided for terminate_task.")
        try:
            auth_headers = await self._get_auth_headers(agent_card, key_manager)
            task_cancel_params = TaskCancelParams(id=task_id)
            request_id = f"req-cancel-{uuid.uuid4()}"
            request_params_dict = task_cancel_params.model_dump(mode='json', by_alias=True)
            request_payload = {"jsonrpc": "2.0", "method": "tasks/cancel", "params": request_params_dict, "id": request_id}
            logger.debug(f"Terminate task request payload (id: {request_id})")
            response_data = await self._make_request('POST', str(agent_card.url), headers=auth_headers, json_payload=request_payload, stream=False)

            if not isinstance(response_data, dict): raise A2AMessageError(f"Invalid response format: Expected dictionary, got {type(response_data)}")
            if "error" in response_data:
                error_data = response_data["error"]; logger.error(f"Agent returned error terminating task {task_id}: {error_data}")
                err_code = error_data.get("code", -1); err_msg = error_data.get("message", "Unknown remote agent error"); err_details = error_data.get("data")
                raise A2ARemoteAgentError(message=err_msg, status_code=err_code, response_body=err_details)
            if "result" not in response_data: raise A2AMessageError("Invalid JSON-RPC response format: missing 'result' key.")
            try:
                result_obj = TaskCancelResult.model_validate(response_data["result"])
                if not result_obj.success: logger.warning(f"Agent acknowledged termination request for task {task_id} but indicated failure (success=false). Message: {result_obj.message}")
            except pydantic.ValidationError as e: raise A2AMessageError(f"Failed to validate terminate task result structure: {e}") from e
            logger.info(f"Termination request for task {task_id} acknowledged by agent {agent_card.human_readable_id}.")
            return True
        except (A2AAuthenticationError, A2AConnectionError, A2ARemoteAgentError, A2AMessageError, A2ATimeoutError) as e: logger.error(f"A2A error terminating task {task_id}: {type(e).__name__}: {e}"); raise
        except KeyManagementError as e: logger.error(f"Key management error terminating task {task_id}: {e}"); raise A2AAuthenticationError(f"Authentication failed due to key management error: {e}") from e
        except Exception as e: logger.exception(f"Unexpected error terminating task {task_id} on agent {agent_card.human_readable_id}: {e}"); raise A2AError(f"An unexpected error occurred terminating task: {e}") from e


    # --- Private Helper Methods ---
    async def _get_auth_headers(self, agent_card: AgentCard, key_manager: KeyManager) -> Dict[str, str]:
        """Determines required auth scheme and retrieves key/token if needed."""
        agent_schemes = agent_card.auth_schemes
        supported_schemes_str = [s.scheme for s in agent_schemes]
        logger.debug(f"Agent supports auth schemes: {supported_schemes_str}")

        # Check for apiKey
        api_key_scheme = next((s for s in agent_schemes if s.scheme == 'apiKey'), None)
        if api_key_scheme:
            service_id = api_key_scheme.service_identifier or agent_card.human_readable_id
            if not service_id: raise A2AAuthenticationError(f"Cannot determine service identifier for apiKey scheme on agent {agent_card.human_readable_id}.")
            logger.debug(f"Attempting to retrieve key for service_id '{service_id}' using apiKey scheme.")
            api_key = key_manager.get_key(service_id)
            if not api_key: raise A2AAuthenticationError(f"Missing API key for service '{service_id}' required by agent '{agent_card.human_readable_id}' (scheme: apiKey). Check local configuration.")
            logger.debug(f"Using apiKey scheme for service_id '{service_id}'.")
            return {"X-Api-Key": api_key}

        # Check for oauth2
        oauth2_scheme = next((s for s in agent_schemes if s.scheme == 'oauth2'), None)
        if oauth2_scheme:
            logger.debug("Found 'oauth2' authentication scheme. Attempting Client Credentials Grant.")
            service_id = oauth2_scheme.service_identifier or agent_card.human_readable_id
            if not service_id: raise A2AAuthenticationError(f"Cannot determine service identifier for oauth2 scheme on agent {agent_card.human_readable_id}.")
            if not oauth2_scheme.token_url: raise A2AAuthenticationError(f"Agent card specifies oauth2 scheme but is missing 'tokenUrl' for agent {agent_card.human_readable_id}.")

            # --- ADDED: Token Caching Logic ---
            now = time.time()
            cached_token_info = self._token_cache.get(service_id)
            if cached_token_info:
                token, expiry = cached_token_info
                if expiry is None or expiry > now:
                    logger.debug(f"Using cached OAuth token for service '{service_id}'.")
                    return {"Authorization": f"Bearer {token}"}
                else:
                    logger.debug(f"Cached OAuth token for service '{service_id}' expired. Fetching new token.")
            # --- END ADDED ---

            logger.debug(f"Retrieving OAuth credentials for service_id '{service_id}'.")
            client_id = key_manager.get_oauth_client_id(service_id)
            client_secret = key_manager.get_oauth_client_secret(service_id)

            if not client_id or not client_secret:
                raise A2AAuthenticationError(f"Missing OAuth Client ID or Client Secret for service '{service_id}'. Check local configuration.")

            token_url_str = str(oauth2_scheme.token_url)

            token_data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
            if oauth2_scheme.scopes:
                token_data["scope"] = " ".join(oauth2_scheme.scopes)

            token_headers = {"Content-Type": "application/x-www-form-urlencoded"}

            logger.debug(f"Requesting OAuth token from {token_url_str} for service '{service_id}'.")
            try:
                response = await self._http_client.request(
                    method="POST",
                    url=token_url_str,
                    data=token_data,
                    headers=token_headers,
                    timeout=self.default_timeout
                )
                response.raise_for_status() # Check for HTTP errors

                try:
                    token_response_data = response.json()
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode JSON response from token endpoint {token_url_str}: {e}. Response: {response.text[:200]}...")
                    raise A2AAuthenticationError(f"Invalid JSON response from token endpoint {token_url_str}.") from e

                access_token = token_response_data.get("access_token")
                token_type = token_response_data.get("token_type", "bearer") # Default to bearer if missing

                if not access_token or not isinstance(access_token, str):
                    logger.error(f"Token response from {token_url_str} missing 'access_token'. Response: {token_response_data}")
                    raise A2AAuthenticationError(f"Invalid token response from {token_url_str}: missing 'access_token'.")
                if token_type.lower() != "bearer":
                    logger.warning(f"Token response from {token_url_str} has non-Bearer token_type: '{token_type}'. Proceeding anyway.")

                logger.info(f"Successfully obtained OAuth token for service '{service_id}'.")

                # --- ADDED: Cache the new token ---
                expires_in = token_response_data.get("expires_in")
                expiry_timestamp: Optional[float] = None
                if isinstance(expires_in, (int, float)) and expires_in > 0:
                    expiry_timestamp = time.time() + expires_in - CACHE_EXPIRY_BUFFER_SECONDS
                    logger.debug(f"Caching token for service '{service_id}' with expiry {expiry_timestamp} (expires_in={expires_in}s).")
                else:
                    logger.debug(f"Caching token for service '{service_id}' without expiry.")
                self._token_cache[service_id] = (access_token, expiry_timestamp)
                # --- END ADDED ---

                return {"Authorization": f"Bearer {access_token}"}

            except httpx.TimeoutException as e:
                logger.error(f"Timeout requesting OAuth token from {token_url_str}: {e}")
                raise A2AAuthenticationError(f"Timeout connecting to token endpoint {token_url_str}.") from e
            except httpx.ConnectError as e:
                logger.error(f"Connection error requesting OAuth token from {token_url_str}: {e}")
                raise A2AAuthenticationError(f"Could not connect to token endpoint {token_url_str}.") from e
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                error_detail = e.response.text[:200] # Limit error detail length
                logger.error(f"HTTP error {status_code} from token endpoint {token_url_str}: {error_detail}")
                if status_code in [400, 401, 403]:
                    raise A2AAuthenticationError(f"Invalid credentials or request for token endpoint {token_url_str} (HTTP {status_code}): {error_detail}") from e
                else: # 5xx or other errors
                    raise A2AAuthenticationError(f"Token endpoint {token_url_str} returned server error (HTTP {status_code}): {error_detail}") from e
            except httpx.RequestError as e:
                logger.error(f"Network error requesting OAuth token from {token_url_str}: {e}")
                raise A2AAuthenticationError(f"Network error communicating with token endpoint {token_url_str}.") from e
            except Exception as e: # Catch any other unexpected error
                logger.exception(f"Unexpected error during OAuth token request to {token_url_str}: {e}")
                raise A2AAuthenticationError(f"Unexpected error during OAuth token request: {e}") from e

        # Check for none
        none_scheme_present = any(s.scheme == 'none' for s in agent_schemes)
        if none_scheme_present:
            logger.debug("Using 'none' authentication scheme."); return {}

        # No compatible scheme found
        client_supported = ['apiKey', 'oauth2', 'none'] # Update client supported list
        log_msg = (f"No compatible authentication scheme found for agent {agent_card.human_readable_id}. "
                   f"Agent supports: {supported_schemes_str}. Client supports: {client_supported}.")
        logger.error(log_msg); raise A2AAuthenticationError(log_msg)

    async def _stream_request(
        self, request_kwargs: Dict[str, Any]
    ) -> AsyncGenerator[bytes, None]:
        """Handles the actual streaming request and yields bytes."""
        method = request_kwargs.get("method", "UNKNOWN"); url = request_kwargs.get("url", "UNKNOWN_URL"); log_context = f"{method} {url} (stream)"
        logger.debug(f"Initiating stream request: {log_context}")
        try:
            async with self._http_client.stream(**request_kwargs) as response:
                try:
                    response.raise_for_status(); logger.debug(f"Stream request successful ({response.status_code}) for {log_context}, yielding byte stream.")
                    async for chunk in response.aiter_bytes(): yield chunk; return
                except httpx.HTTPStatusError as e:
                    await response.aread(); logger.error(f"HTTP error on stream request {log_context}: {e.response.status_code}")
                    raise A2ARemoteAgentError(message=f"HTTP error {e.response.status_code} for {url}: {e.response.text}", status_code=e.response.status_code, response_body=e.response.text) from e
        except httpx.TimeoutException as e: logger.error(f"Request timeout for {log_context}: {e}"); raise A2ATimeoutError(f"Request timed out for {url}: {e}") from e
        except httpx.ConnectError as e: logger.error(f"Connection error for {log_context}: {e}"); raise A2AConnectionError(f"Connection failed for {url}: {e}") from e
        except httpx.RequestError as e: logger.error(f"HTTP request error for {log_context}: {e}"); raise A2AConnectionError(f"HTTP request failed for {url}: {e}") from e
        except Exception as e: logger.exception(f"Unexpected error during stream request {log_context}: {e}"); raise A2AError(f"An unexpected error occurred during the stream request for {url}: {e}") from e

    async def _make_request(
        self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
        json_payload: Optional[Dict[str, Any]] = None, stream: bool = False
    ) -> Union[Dict[str, Any], AsyncGenerator[bytes, None]]:
        """Internal helper to make HTTP requests using the configured httpx client."""
        url_str = str(url)
        request_kwargs = {"method": method, "url": url_str, "headers": headers or {}, "json": json_payload}; log_context = f"{method} {url_str}"
        if stream: return self._stream_request(request_kwargs)
        else:
            logger.debug(f"Making non-stream request: {log_context}");
            if json_payload: logger.debug(f"Request payload keys: {list(json_payload.keys())}")
            try:
                response = await self._http_client.request(**request_kwargs); response.raise_for_status()
                try:
                    response_data = response.json(); log_resp_str = f"size={len(response.content)} bytes, keys={list(response_data.keys())}" if isinstance(response_data, dict) else f"size={len(response.content)} bytes"; logger.debug(f"Request successful ({response.status_code}) for {log_context}. Response: {log_resp_str}"); return response_data
                except json.JSONDecodeError as e: logger.error(f"Failed to decode JSON response from {log_context}. Status: {response.status_code}. Response text: {response.text[:200]}..."); raise A2AMessageError(f"Failed to decode JSON response from {url_str}. Status: {response.status_code}. Body: {response.text[:200]}...") from e
            except httpx.TimeoutException as e: logger.error(f"Request timeout for {log_context}: {e}"); raise A2ATimeoutError(f"Request timed out for {url_str}: {e}") from e
            except httpx.ConnectError as e: logger.error(f"Connection error for {log_context}: {e}"); raise A2AConnectionError(f"Connection failed for {url_str}: {e}") from e
            except httpx.HTTPStatusError as e: logger.error(f"HTTP error on request {log_context}: {e.response.status_code}"); raise A2ARemoteAgentError(message=f"HTTP error {e.response.status_code} for {url_str}: {e.response.text}", status_code=e.response.status_code, response_body=e.response.text) from e
            except httpx.RequestError as e: logger.error(f"HTTP request error for {log_context}: {e}"); raise A2AConnectionError(f"HTTP request failed for {url_str}: {e}") from e
            except (A2AMessageError, A2AAuthenticationError, A2ARemoteAgentError) as e: logger.error(f"A2A error during request processing for {log_context}: {e}"); raise
            except Exception as e: logger.exception(f"Unexpected error during request {log_context}: {e}"); raise A2AError(f"An unexpected error occurred during the request for {url_str}: {e}") from e

    async def _process_sse_stream(self, byte_stream: AsyncGenerator[bytes, None]) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Processes a Server-Sent Event byte stream into event dictionaries.
        Handles event type, data fields, and basic error checking.
        """
        buffer = ""
        current_event_type = None
        data_buffer = ""
        try:
            async for chunk in byte_stream:
                try:
                    buffer += chunk.decode('utf-8')
                except UnicodeDecodeError:
                    logger.warning("Received non-UTF8 chunk in SSE stream, skipping.")
                    buffer = "" # Clear buffer on decode error
                    continue

                # Process lines separated by \n or \r or \r\n
                while True:
                    newline_pos = -1
                    if '\n' in buffer: newline_pos = buffer.find('\n')
                    if '\r' in buffer:
                        cr_pos = buffer.find('\r')
                        if newline_pos == -1 or cr_pos < newline_pos:
                            newline_pos = cr_pos

                    if newline_pos == -1: break # No complete line in buffer yet

                    line = buffer[:newline_pos]
                    # Handle different line endings (\n, \r, \r\n)
                    if buffer[newline_pos] == '\r' and newline_pos + 1 < len(buffer) and buffer[newline_pos+1] == '\n':
                        buffer = buffer[newline_pos+2:] # Skip \r\n
                    else:
                        buffer = buffer[newline_pos+1:] # Skip \n or \r

                    if not line: # Empty line signifies end of an event
                        if data_buffer:
                            event_type = current_event_type or "message" # Default type is 'message'
                            logger.debug(f"Received SSE event: type='{event_type}', data='{data_buffer[:100]}...'")
                            try:
                                # Yield the parsed event data
                                yield {"event_type": event_type, "data": json.loads(data_buffer)}
                            except json.JSONDecodeError as e:
                                logger.error(f"Failed to decode JSON data for SSE event type '{event_type}': {e}. Data: {data_buffer[:200]}...")
                                # Continue to next event, don't yield malformed data
                            # Reset for next event
                            data_buffer = ""
                            current_event_type = None
                        continue # Process next line

                    if line.startswith(':'): # Ignore comments
                        continue

                    # Parse field: value lines
                    try:
                        field, value = line.split(":", 1)
                        value = value.lstrip() # Remove leading space from value

                        if field == "event":
                            current_event_type = value
                        elif field == "data":
                            # Append data, adding newline if data_buffer already has content
                            data_buffer += ("\n" if data_buffer else "") + value
                        elif field == "id": # Optional event ID field (ignored for now)
                            pass
                        elif field == "retry": # Optional retry field (ignored for now)
                            pass
                        else:
                            logger.warning(f"Ignoring unknown SSE field: '{field}'")
                    except ValueError:
                        logger.warning(f"Ignoring malformed SSE line (no colon): {line}")

            # Process any remaining buffer content after stream ends
            if data_buffer:
                event_type = current_event_type or "message"
                logger.debug(f"Processing final SSE event buffer: type='{event_type}', data='{data_buffer[:100]}...'")
                try:
                    yield {"event_type": event_type, "data": json.loads(data_buffer)}
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode JSON data for final SSE event type '{event_type}': {e}. Data: {data_buffer[:200]}...")

        except httpx.RemoteProtocolError as e:
            logger.error(f"SSE stream ended unexpectedly (RemoteProtocolError): {e}", exc_info=True)
            raise A2AConnectionError(f"SSE stream ended unexpectedly: {e}") from e
        except httpx.StreamError as e:
            logger.error(f"Error reading from SSE stream: {e}", exc_info=True)
            raise A2AConnectionError(f"Error reading from SSE stream: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error processing SSE stream: {e}", exc_info=True)
            raise A2AConnectionError(f"Unexpected error processing SSE stream: {e}") from e
        finally:
            logger.debug("SSE byte stream processing finished.")

#
