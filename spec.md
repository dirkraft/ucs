The point of this is to map the idea of an agent session to be 1-1 with a particular docker container.
Rather than try to use built in notions of session in claude code, or other agent platforms, we treat the entire 
contained environment as one session.

When a user mentions @pal in slack, the dispatcher process...
* checks that the event is from an authorized user. If it is not, the event is ignored. Only authorized users can trigger agent activity.
* checks if docker container named ucs_sess_slack_${thread_id} exists, if it doesn't, create and start it with sleep infinity in the background.
  * The docker image: debian base with the claude code cli installed. The image should be called ucs_agent_claude
  * credentials are made available by copying ~/.claude/.credentials.json into the container
* if the container is already running and the contained agent process is active, the process is interrupted! so we can restart it with the latest event. ideally gracefully, so that the agent doesn't end up with corrupted edits to the filesystem whether that is to project files or its own agent state files. 
  * If interruption is necessary, then post "interrupting..." to the placeholder slack message
* once interruption is complete, or if not necessary at all and we're ready to begin thinking, the placeholder message should be changed to say "thinking..."
* then docker exec the following command: claude --dangerously-skip-permissions --output-format stream-json --print --verbose claude_options... prompt (see --help)
  * prompt - we will have to iterate on this. At minimum it contains the last user message, but we might create a structure to indicate some metadata
  * --system-prompt - we may want to use this to provide framing for how claude is being invoked so that it can reason and act well, or problaby --append-system-prompt is better for anything we want to add. The idea is to keep the famliar cc experience but also wrap it up in this container-is-a-session concept
  * --plugin-dir - wonder if this is useful for the contained agent to interact with the dispatcher, e.g. request more memory, mount another volume (which would require a series of docker commands docker commit ucs_sess_slack_${thread_id} ; docker rm existing container... ; docker create new container ; etc...). We'll work on this later. On that note, if it is easy to, the default container resource limit should be 4GB of memory.
* parse the output as it happens
  * major reasoning steps are appended into the placeholder message as bullets (edit the standing slack message), something like this
    thinking...
    * reasoning step one
    * reasoning step two
  * once the final response is generated, the contents of the placeholder message are replaced with that. The reasoning steps are replaced because while they provide useful feedback to the user that progress is happening, they can take up a lot of space, so we only want the final response in the message. Later on we'll develop an agent HUD where detailed logs are available so they can still be seen but don't clutter up slack.

Slack bot credentials are already available for the dispatcher (through .envrc -> .env)