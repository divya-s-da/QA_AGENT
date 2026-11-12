      # Convert the events to properly formatted SSE
      async def event_generator():
        # Sisyphus patch (SSE keepalive): with streaming disabled the
        # SSE connection is completely silent during long LLM calls.
        # Intermediaries (NAT/proxy/ngrok edge) drop idle connections
        # after ~300s, which cancels the run and surfaces as a
        # "network error" in the UI. Emit SSE comment keepalives so
        # the connection is never idle. Comments are ignored by
        # EventSource and by the dev-ui parser (it filters data: lines).
        keepalive_seconds = 20
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        async def _consume() -> None:
          try:
            async with Aclosing(
                runner.run_async(
                    user_id=req.user_id,
                    session_id=req.session_id,
                    new_message=req.new_message,
                    state_delta=req.state_delta,
                    run_config=RunConfig(
                        streaming_mode=stream_mode,
                        custom_metadata=req.custom_metadata,
                    ),
                    invocation_id=req.invocation_id,
                )
            ) as agen:
              async for event in agen:
                await queue.put(("event", event))
            await queue.put(("end", None))
          except BaseException as e:  # noqa: BLE001 - forwarded to consumer
            await queue.put(("error", e))
            raise

        consumer = asyncio.create_task(_consume())
        try:
          while True:
            try:
              kind, payload = await asyncio.wait_for(
                  queue.get(), timeout=keepalive_seconds
              )
            except asyncio.TimeoutError:
              yield ": keepalive\n\n"
              continue
            if kind == "end":
              break
            if kind == "error":
              raise payload
            event = payload
            # ADK Web renders artifacts from `actions.artifactDelta`
            # during part processing *and* during action processing
            # 1) the original event with `artifactDelta` cleared (content)
            # 2) a content-less "action-only" event carrying `artifactDelta`
            events_to_stream = [event]
            if (
                not req.function_call_event_id
                and event.actions.artifact_delta
                and event.content
                and event.content.parts
            ):
              content_event = event.model_copy(deep=True)
              content_event.actions.artifact_delta = {}
              artifact_event = event.model_copy(deep=True)
              artifact_event.content = None
              events_to_stream = [content_event, artifact_event]

            for event_to_stream in events_to_stream:
              sse_event = event_to_stream.model_dump_json(
                  exclude_none=True,
                  by_alias=True,
              )
              logger.debug(
                  "Generated event in agent run streaming: %s", sse_event
              )
              yield f"data: {sse_event}\n\n"
        except Exception as e:
          logger.exception("Error in event_generator: %s", e)
          error_details = {
              "error_type": type(e).__name__,
              "error_message": str(e),
              "timestamp": time.time(),
          }
          if logger.isEnabledFor(logging.DEBUG):
            error_details["stacktrace"] = traceback.format_exc()

          yield (
              "data:"
              f" {json.dumps({'error': f'{type(e).__name__}: {e}', 'error_details': error_details})}\n\n"
          )
        finally:
          if not consumer.done():
            consumer.cancel()
          with suppress(asyncio.CancelledError, Exception):
            await consumer
