import json
import logging
import os
import time

from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterator

import requests

from PIL import Image

from .dataclasses import (
    GeometryGenerationError,
    GeometryGenerationServerRequest,
    GeometryGenerationServerResponse,
    StreamedResult,
)

console_logger = logging.getLogger(__name__)


class GeometryGenerationClient:
    """Client for making requests to the geometry generation server.

    Provides a high-level interface for generating 3D geometry from images
    using the geometry generation server. Handles HTTP communication, retries,
    error handling, and response parsing.

    The client maintains a persistent HTTP session for connection pooling
    and includes automatic retry logic with exponential backoff for
    transient failures.

    Example:
        >>> client = GeometryGenerationClient()
        >>> requests = [GeometryGenerationServerRequest(
        ...     image_path="/path/to/image.png",
        ...     output_dir="/path/to/output",
        ...     prompt="Modern wooden chair"
        ... )]
        >>> for index, response in client.generate_geometries(requests):
        ...     print(f"Generated geometry: {response.geometry_path}")
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7000):
        """Initialize geometry generation client.

        Args:
            host: Server hostname or IP address. Should be accessible from
                the current network context. Defaults to localhost.
            port: Server port number. Must match the port where the geometry
                generation server is listening. Defaults to 7000.
        """
        self.mode = os.environ.get("SCENESMITH_GEOMETRY_MODE", "local").lower()
        if self.mode not in {"local", "remote"}:
            raise ValueError("SCENESMITH_GEOMETRY_MODE must be 'local' or 'remote'")
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()
        if self.mode == "remote":
            self.geometry_base_url = _required_env(
                "SCENE_ENGINE_GEOMETRY_GENERATION_BASE_URL"
            ).rstrip("/")
            self.geometry_path = _required_env(
                "SCENE_ENGINE_GEOMETRY_GENERATION_OBJECTS_PATH"
            )
            self.geometry_health_path = os.environ.get(
                "SCENE_ENGINE_GEOMETRY_GENERATION_HEALTH_PATH", "/health"
            )
            self.geometry_timeout_s = _positive_int_env(
                "SCENE_ENGINE_GEOMETRY_GENERATION_TIMEOUT_S", 600
            )
            self.geometry_max_attempts = _positive_int_env(
                "SCENE_ENGINE_GEOMETRY_GENERATION_MAX_ATTEMPTS", 3
            )
            self.segmentation_base_url = _required_env(
                "SCENE_ENGINE_IMAGE_SEGMENTATION_BASE_URL"
            ).rstrip("/")
            self.segmentation_path = _required_env(
                "SCENE_ENGINE_IMAGE_SEGMENTATION_BY_PROMPT_PATH"
            )
            self.segmentation_health_path = os.environ.get(
                "SCENE_ENGINE_IMAGE_SEGMENTATION_HEALTH_PATH", "/health"
            )
            self.segmentation_timeout_s = _positive_int_env(
                "SCENE_ENGINE_IMAGE_SEGMENTATION_TIMEOUT_S", 30
            )
            self.segmentation_max_attempts = _positive_int_env(
                "SCENE_ENGINE_IMAGE_SEGMENTATION_MAX_ATTEMPTS", 3
            )
        console_logger.debug(
            "Geometry generation client initialized in %s mode", self.mode
        )

    def generate_geometries(
        self,
        geometry_requests: list[GeometryGenerationServerRequest],
        max_retries: int = 3,
        timeout_s: int = 3600,
    ) -> Iterator[
        tuple[int, GeometryGenerationServerResponse | GeometryGenerationError]
    ]:
        """Send batch geometry generation requests and yield results as they complete.

        Submits a batch of geometry generation requests to the server and yields
        results as they stream back. This enables pipelining where the client can
        start processing earlier results while the server continues working on
        later requests.

        Individual request failures are yielded as GeometryGenerationError objects
        rather than raising exceptions, allowing the batch to continue processing.

        Args:
            geometry_requests: List of geometry generation requests to process as a batch.
            max_retries: Maximum number of retries for transient failures.
            timeout_s: Timeout in seconds for the entire batch. Should scale with
                batch size and expected server queue depth.

        Yields:
            Tuple of (index, result) where index corresponds to the request's
            position in the input list and result is either:
            - GeometryGenerationServerResponse: Contains the generated geometry path
            - GeometryGenerationError: Contains error details for failed requests

        Raises:
            ConnectionError: If unable to connect to server after max retries.
            RuntimeError: If server returns invalid data (e.g., malformed JSON).
            TimeoutError: If request exceeds timeout limit.
            ValueError: If the requests list is empty.
        """
        if not geometry_requests:
            raise ValueError("Requests list cannot be empty")

        if self.mode == "remote":
            yield from self._generate_geometries_remote(geometry_requests)
            return

        yield from self._generate_geometries_local(
            geometry_requests, max_retries=max_retries, timeout_s=timeout_s
        )

    def _generate_geometries_local(
        self,
        geometry_requests: list[GeometryGenerationServerRequest],
        max_retries: int,
        timeout_s: int,
    ) -> Iterator[
        tuple[int, GeometryGenerationServerResponse | GeometryGenerationError]
    ]:
        """Use SceneSmith's original local streaming server protocol."""

        for attempt in range(max_retries):
            try:
                console_logger.debug(
                    f"Sending batch request (attempt {attempt + 1}) with "
                    f"{len(geometry_requests)} requests"
                )

                # Prepare request payload.
                request_data = [req.to_dict() for req in geometry_requests]

                # Send streaming request.
                http_response = self.session.post(
                    f"{self.base_url}/generate_geometries",
                    json=request_data,
                    stream=True,
                    timeout=(10, timeout_s),  # 10s connect, timeout_s read
                )
                http_response.raise_for_status()

                # Parse streaming NDJSON response.
                for line in http_response.iter_lines():
                    if line:
                        try:
                            result_data = json.loads(line.decode("utf-8"))
                            streamed_result = StreamedResult(**result_data)

                            if streamed_result.status == "error":
                                # Yield error and continue processing remaining results.
                                console_logger.warning(
                                    f"Geometry generation failed for request "
                                    f"{streamed_result.index}: {streamed_result.error}"
                                )
                                yield streamed_result.index, GeometryGenerationError(
                                    index=streamed_result.index,
                                    error_message=streamed_result.error
                                    or "Unknown error",
                                )
                                continue

                            # Convert to response object.
                            response = GeometryGenerationServerResponse(
                                **streamed_result.data
                            )
                            yield streamed_result.index, response

                        except json.JSONDecodeError as e:
                            raise RuntimeError(
                                f"Invalid JSON in streaming response: {e}"
                            ) from e

                console_logger.debug("Batch request completed successfully")
                return  # Success, exit retry loop

            except requests.exceptions.ConnectionError as e:
                if attempt < max_retries - 1:
                    console_logger.warning(
                        f"Connection failed, retrying... ({attempt + 1}/{max_retries})"
                    )
                    time.sleep(min(2**attempt, 60))  # Exponential backoff with max 60s
                else:
                    console_logger.error("Asset server connection failed after retries")
                    raise ConnectionError(
                        f"Failed to connect to asset server at {self.base_url}"
                    ) from e

            except requests.exceptions.HTTPError as e:
                if e.response.status_code >= 500:
                    # Server error, might be temporary.
                    if attempt < max_retries - 1:
                        console_logger.warning(
                            f"Server error, retrying... ({attempt + 1}/{max_retries})"
                        )
                        time.sleep(2**attempt)
                        continue

                # Client error or persistent server error.
                try:
                    error_detail = e.response.json()["error"]
                except (KeyError, ValueError):
                    error_detail = str(e)
                console_logger.error(f"HTTP error from asset server: {error_detail}")
                raise RuntimeError(f"Asset server error: {error_detail}") from e

            except requests.exceptions.Timeout as e:
                console_logger.error("Batch asset generation request timed out")
                raise TimeoutError("Batch asset generation request timed out") from e

    def _generate_geometries_remote(
        self, geometry_requests: list[GeometryGenerationServerRequest]
    ) -> Iterator[
        tuple[int, GeometryGenerationServerResponse | GeometryGenerationError]
    ]:
        """Run prompt segmentation then masked geometry generation via cloud APIs."""
        for index, geometry_request in enumerate(geometry_requests):
            try:
                image_path = Path(geometry_request.image_path).expanduser().resolve()
                if not image_path.is_file():
                    raise FileNotFoundError(
                        f"Geometry generation input not found: {image_path}"
                    )
                output_dir = Path(geometry_request.output_dir).expanduser().resolve()
                output_dir.mkdir(parents=True, exist_ok=True)
                output_filename = geometry_request.output_filename or "geometry.glb"
                output_filename = Path(output_filename).name
                if not output_filename.lower().endswith(".glb"):
                    output_filename += ".glb"
                object_id = Path(output_filename).stem

                mask_dir = (
                    Path(geometry_request.debug_folder).expanduser().resolve()
                    if geometry_request.debug_folder
                    else output_dir / "masks"
                )
                mask_dir.mkdir(parents=True, exist_ok=True)
                mask_path = mask_dir / f"{object_id}_mask.png"
                self._segment_to_mask(
                    image_path=image_path,
                    prompt=geometry_request.prompt,
                    mask_path=mask_path,
                )
                output_path = output_dir / output_filename
                self._generate_remote_geometry(
                    image_path=image_path,
                    mask_path=mask_path,
                    object_id=object_id,
                    output_path=output_path,
                )
                yield index, GeometryGenerationServerResponse(
                    geometry_path=str(output_path)
                )
            except Exception as exc:
                console_logger.exception(
                    "Remote geometry generation failed for request %d", index
                )
                yield index, GeometryGenerationError(
                    index=index, error_message=str(exc)
                )

    def _segment_to_mask(
        self, *, image_path: Path, prompt: str, mask_path: Path
    ) -> None:
        last_error: Exception | None = None
        url = _join_url(self.segmentation_base_url, self.segmentation_path)
        for attempt in range(self.segmentation_max_attempts):
            try:
                with image_path.open("rb") as image_file:
                    response = self.session.post(
                        url,
                        data={"prompt": prompt},
                        files={"image": (image_path.name, image_file)},
                        timeout=self.segmentation_timeout_s,
                    )
                response.raise_for_status()
                response_data = response.json()
                if not isinstance(response_data, dict):
                    raise RuntimeError(
                        "Image segmentation response must be a JSON object"
                    )
                if response_data.get("ok") is False:
                    raise RuntimeError(
                        "Image segmentation failed: "
                        f"{response_data.get('error', 'unknown error')}"
                    )
                masks = _extract_rle_masks(response_data)
                if not masks:
                    raise RuntimeError("Image segmentation returned no masks")
                mask = _decode_rle_mask(masks[0])
                mask = _ensure_centered_foreground(mask)
                mask.save(mask_path, format="PNG")
                return
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < self.segmentation_max_attempts:
                    time.sleep(min(2**attempt, 10))
        raise RuntimeError(
            "Image segmentation failed after "
            f"{self.segmentation_max_attempts} attempts"
        ) from last_error

    def _generate_remote_geometry(
        self,
        *,
        image_path: Path,
        mask_path: Path,
        object_id: str,
        output_path: Path,
    ) -> None:
        last_error: Exception | None = None
        url = _join_url(self.geometry_base_url, self.geometry_path)
        for attempt in range(self.geometry_max_attempts):
            try:
                with ExitStack() as stack:
                    image_file = stack.enter_context(image_path.open("rb"))
                    mask_file = stack.enter_context(mask_path.open("rb"))
                    response = self.session.post(
                        url,
                        files=[
                            (
                                "image",
                                (image_path.name, image_file, _image_type(image_path)),
                            ),
                            (
                                "masks",
                                (f"{object_id}.png", mask_file, "image/png"),
                            ),
                        ],
                        timeout=self.geometry_timeout_s,
                    )
                response.raise_for_status()
                response_data = response.json()
                response_data = self._wait_for_task_if_needed(response_data)
                mesh_path = _extract_mesh_path(response_data, object_id)
                self._download_glb(mesh_path, output_path)
                return
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < self.geometry_max_attempts:
                    time.sleep(min(2**attempt, 10))
        raise RuntimeError(
            "Geometry generation failed after " f"{self.geometry_max_attempts} attempts"
        ) from last_error

    def _wait_for_task_if_needed(self, response_data: object) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            raise RuntimeError("Geometry generation response must be a JSON object")
        status = response_data.get("status")
        if not isinstance(status, str) or "waiting" not in status:
            return response_data
        request_id = response_data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("Queued geometry response has no request_id")
        for _ in range(600):
            response = self.session.get(
                _join_url(self.geometry_base_url, f"/tasks/{request_id}"), timeout=10
            )
            response.raise_for_status()
            task_data = response.json()
            if not isinstance(task_data, dict):
                raise RuntimeError("Geometry task response must be a JSON object")
            task_status = task_data.get("status")
            if task_status == "succeeded":
                return task_data
            if task_status in {"failed", "cancelled"}:
                raise RuntimeError(
                    f"Geometry task {task_status}: "
                    f"{task_data.get('error', 'unknown error')}"
                )
            if not isinstance(task_status, str) or (
                task_status != "running" and "waiting" not in task_status
            ):
                raise RuntimeError(f"Unknown geometry task status: {task_status!r}")
            time.sleep(1)
        raise RuntimeError(f"Geometry task timed out: {request_id}")

    def _download_glb(self, mesh_path: str, output_path: Path) -> None:
        mesh_url = (
            mesh_path
            if mesh_path.startswith(("http://", "https://"))
            else _join_url(self.geometry_base_url, mesh_path)
        )
        response = self.session.get(mesh_url, timeout=self.geometry_timeout_s)
        response.raise_for_status()
        if not response.content.startswith(b"glTF"):
            raise RuntimeError("Geometry server returned invalid GLB content")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)

    def health_check(self) -> bool:
        """Check if the geometry generation server is healthy and responsive.

        Returns:
            True if server responds successfully to health check within
            5 seconds, False if server is unreachable, returns an error,
            or times out.
        """
        try:
            if self.mode == "remote":
                health_urls = [
                    _join_url(
                        self.segmentation_base_url, self.segmentation_health_path
                    ),
                    _join_url(self.geometry_base_url, self.geometry_health_path),
                ]
            else:
                health_urls = [f"{self.base_url}/health"]
            for health_url in health_urls:
                response = self.session.get(health_url, timeout=5)
                response.raise_for_status()
            return True
        except Exception as e:
            console_logger.warning(f"Health check failed: {e}")
            return False


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Required environment variable is not set: {name}")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _extract_rle_masks(response_data: dict[str, Any]) -> list[dict[str, Any]]:
    result_data = response_data.get("result") or response_data.get("data")
    if not isinstance(result_data, dict):
        result_data = response_data
    masks = result_data.get("masks")
    if isinstance(masks, list):
        parsed = [mask for mask in masks if isinstance(mask, dict)]
        if parsed:
            return parsed
    instances = result_data.get("instances", [])
    if not isinstance(instances, list):
        return []
    parsed = []
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        mask = (
            instance.get("mask_rle")
            or instance.get("mask")
            or instance.get("segmentation")
        )
        if isinstance(mask, dict):
            parsed.append(mask)
    return parsed


def _decode_rle_mask(mask_rle: dict[str, Any]) -> Image.Image:
    size = mask_rle.get("size")
    counts = mask_rle.get("counts")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or not all(isinstance(value, int) and value > 0 for value in size)
    ):
        raise ValueError("RLE mask needs size=[height, width]")
    if not isinstance(counts, list):
        raise ValueError("RLE mask counts must be an uncompressed list")
    height, width = size
    pixel_count = height * width
    starts_with = mask_rle.get("starts_with", 0)
    if starts_with not in (0, 1, False, True):
        raise ValueError("RLE starts_with must be 0 or 1")
    pixels = bytearray(pixel_count)
    foreground = bool(starts_with)
    offset = 0
    for raw_count in counts:
        if isinstance(raw_count, bool):
            raise ValueError("RLE counts must contain integers")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("RLE counts must contain integers") from exc
        if count < 0 or offset + count > pixel_count:
            raise ValueError("RLE counts do not match the declared size")
        if foreground:
            pixels[offset : offset + count] = b"\xff" * count
        offset += count
        foreground = not foreground
    if offset != pixel_count:
        raise ValueError("RLE does not cover the image")
    return Image.frombytes("L", (width, height), bytes(pixels))


def _ensure_centered_foreground(mask: Image.Image) -> Image.Image:
    width, height = mask.size
    left, top = width // 6, height // 6
    right, bottom = width - left, height - top
    center = mask.crop((left, top, right, bottom))
    center_ratio = sum(center.histogram()[128:]) / (center.width * center.height)
    total_foreground = sum(mask.histogram()[128:])
    center_foreground = sum(center.histogram()[128:])
    outside_pixels = width * height - center.width * center.height
    if outside_pixels == 0:
        return mask
    outside_ratio = (total_foreground - center_foreground) / outside_pixels
    if center_ratio >= outside_ratio:
        return mask
    return mask.point(lambda value: 0 if value else 255)


def _extract_mesh_path(response_data: dict[str, Any], object_id: str) -> str:
    if response_data.get("ok") is not True:
        raise RuntimeError(
            "Geometry generation failed: "
            f"{response_data.get('error', 'ok is not true')}"
        )
    result = response_data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Geometry response has no result object")
    objects = result.get("objects")
    if not isinstance(objects, list) or len(objects) != 1:
        raise RuntimeError("Geometry response must contain exactly one object")
    response_object = objects[0]
    if not isinstance(response_object, dict):
        raise RuntimeError("Geometry response object is invalid")
    if response_object.get("name") != object_id:
        raise RuntimeError("Geometry response object name does not match the mask")
    mesh_path = response_object.get("mesh")
    if not isinstance(mesh_path, str) or not mesh_path:
        raise RuntimeError("Geometry response object has no mesh path")
    return mesh_path


def _image_type(image_path: Path) -> str:
    return (
        "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    )
