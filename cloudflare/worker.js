/**
 * HTTP Shortcuts control plane for the GitHub Actions transfer workflow.
 * Cloudflare Worker secrets: GH_TOKEN and API_KEY.
 */
const OWNER = "Hus-GPT";
const REPO = "drive-saver-bot";
const WORKFLOW = "download-to-drive.yml";
const WORKFLOW_REF = "main";
const GH_API = "https://api.github.com";

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET, POST, OPTIONS",
      "access-control-allow-headers": "authorization, content-type",
    },
  });
}

function authorized(request, env) {
  const auth = request.headers.get("authorization") || "";
  return Boolean(env.API_KEY && auth === ("Bearer " + env.API_KEY));
}

async function github(path, env, options = {}) {
  if (!env.GH_TOKEN) throw new Error("Missing Cloudflare secret GH_TOKEN");
  const headers = {
    "accept": "application/vnd.github+json",
    "authorization": "Bearer " + env.GH_TOKEN,
    "x-github-api-version": "2022-11-28",
    "user-agent": "github-download-control-worker",
  };
  if (options.body) headers["content-type"] = "application/json";
  Object.assign(headers, options.headers || {});
  const response = await fetch(GH_API + path, { ...options, headers });
  if (!response.ok) {
    const details = (await response.text()).slice(0, 800);
    throw new Error("GitHub API returned " + response.status + ": " + details);
  }
  if (response.status === 204) return null;
  return response.json();
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET, POST, OPTIONS",
          "access-control-allow-headers": "authorization, content-type",
        },
      });
    }
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") {
      return json({ ok: true, service: "github-download-control", authRequired: true });
    }
    if (!authorized(request, env)) return json({ error: "Unauthorized" }, 401);

    try {
      if (url.pathname === "/start" && request.method === "POST") {
        const body = await request.json().catch(() => null);
        if (!body || typeof body.url !== "string" || !body.url.trim()) {
          return json({ error: "Send JSON with a non-empty url field." }, 400);
        }
        if (body.url.length > 4000) return json({ error: "URL is too long." }, 400);
        let parsed;
        try { parsed = new URL(body.url); } catch { return json({ error: "Invalid URL." }, 400); }
        if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password) {
          return json({ error: "Only HTTP/HTTPS URLs without embedded credentials are accepted." }, 400);
        }
        const inputs = { url: body.url.trim() };
        if (typeof body.filename === "string" && body.filename.trim()) {
          inputs.filename = body.filename.trim().slice(0, 180);
        }
        await github("/repos/" + OWNER + "/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches", env, {
          method: "POST",
          body: JSON.stringify({ ref: WORKFLOW_REF, inputs }),
        });
        return json({
          accepted: true,
          message: "Workflow dispatch accepted. Query /status to inspect recent runs.",
          workflow: WORKFLOW,
          ref: WORKFLOW_REF,
        }, 202);
      }

      if (url.pathname === "/status" && request.method === "GET") {
        const result = await github(
          "/repos/" + OWNER + "/" + REPO + "/actions/workflows/" + WORKFLOW + "/runs?per_page=5&branch=" + encodeURIComponent(WORKFLOW_REF),
          env,
        );
        const runs = (result.workflow_runs || []).map(run => ({
          id: run.id,
          name: run.name,
          status: run.status,
          conclusion: run.conclusion,
          created_at: run.created_at,
          updated_at: run.updated_at,
          html_url: run.html_url,
          run_number: run.run_number,
          event: run.event,
          branch: run.head_branch,
        }));
        return json({ workflow: WORKFLOW, runs });
      }

      if (url.pathname === "/status" && request.method === "POST") {
        const body = await request.json().catch(() => ({}));
        const runId = String(body.run_id || "");
        if (!/^\d+$/.test(runId)) return json({ error: "Provide a numeric run_id." }, 400);
        const run = await github("/repos/" + OWNER + "/" + REPO + "/actions/runs/" + runId, env);
        if (run.repository && run.repository.full_name !== OWNER + "/" + REPO) {
          return json({ error: "Run does not belong to the configured repository." }, 403);
        }
        const jobs = await github("/repos/" + OWNER + "/" + REPO + "/actions/runs/" + runId + "/jobs?per_page=20", env);
        return json({
          id: run.id,
          status: run.status,
          conclusion: run.conclusion,
          created_at: run.created_at,
          updated_at: run.updated_at,
          html_url: run.html_url,
          jobs: (jobs.jobs || []).map(job => ({
            name: job.name,
            status: job.status,
            conclusion: job.conclusion,
            steps: (job.steps || []).map(step => ({
              name: step.name,
              status: step.status,
              conclusion: step.conclusion,
            })),
          })),
        });
      }

      return json({
        error: "Not found",
        endpoints: ["POST /start", "GET /status", "POST /status with {run_id}", "GET /health"],
      }, 404);
    } catch (error) {
      return json({ error: error instanceof Error ? error.message : "Unexpected error" }, 502);
    }
  },
};
