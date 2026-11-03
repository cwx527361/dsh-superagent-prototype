package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// execJob runs one offer: worktree, local opencode session, result.
func (r *runner) execJob(parent context.Context, spec JobSpec) {
	timeout := time.Duration(spec.TimeoutMs) * time.Millisecond
	if timeout <= 0 || timeout > 2*time.Hour {
		timeout = 30 * time.Minute
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	r.registerJob(spec, cancel)
	defer r.finishJob(spec.JobID)

	emit := func(kind string, data any) {
		r.emit(spec.JobID, kind, data)
	}
	accept, _ := frame("job.accept", map[string]any{"jobId": spec.JobID})
	if err := r.send(accept); err != nil {
		return
	}
	r.log.Printf("[job %s] accepted: %s", spec.JobID, spec.Ticket)
	emit("status", map[string]any{"phase": "accepted", "ticket": spec.Ticket})

	fail := func(cause string, err error, retryable bool) {
		detail := ""
		if err != nil {
			detail = err.Error()
		}
		r.log.Printf("[job %s] failed (%s): %s", spec.JobID, cause, detail)
		env, _ := frame("job.failed", JobFailure{JobID: spec.JobID, Cause: cause, Detail: detail, Retryable: retryable})
		_ = r.send(env)
	}

	// 1. Worktree.
	emit("status", map[string]any{"phase": "workspace"})
	worktree, baseSHA, err := r.prepareWorktree(ctx, spec)
	if err != nil {
		fail("workspace", err, true)
		return
	}
	r.log.Printf("[job %s] worktree %s", spec.JobID, worktree)
	emit("status", map[string]any{"phase": "running", "worktree": worktree})

	// 2. Local opencode session.
	oc := newOpencode(r.cfg.OpencodeURL, r.cfg.OpencodePassword)
	if _, err := oc.info(); err != nil {
		fail("opencode_unreachable", err, true)
		return
	}
	session, err := oc.createSession(worktree, spec.Model)
	if err != nil {
		fail("opencode_session", err, true)
		return
	}
	emit("status", map[string]any{"phase": "prompt", "session": session.ID, "model": session.Model.ID})
	if err := oc.prompt(session.ID, spec.Prompt); err != nil {
		fail("opencode_prompt", err, true)
		return
	}

	// 3. Wait for the agent, streaming its text back as events.
	deadline := time.Now().Add(timeout)
	emitted := 0
	textEmitted := false
	var last ocSession
	for {
		select {
		case <-ctx.Done():
			_ = oc.abort(session.ID)
			fail("cancelled", errors.New("job cancelled or timed out"), false)
			r.cleanupWorktree(spec, worktree, false)
			return
		case <-time.After(2 * time.Second):
		}
		last, err = oc.session(session.ID)
		if err != nil {
			fail("opencode_session_lost", err, true)
			return
		}
		if msgs, err := oc.messages(session.ID); err == nil {
			for i, msg := range msgs {
				if i < emitted {
					continue
				}
				emitted = i + 1
				for _, part := range msg.Content {
					if part.Type == "text" && strings.TrimSpace(part.Text) != "" {
						textEmitted = true
						emit("log", map[string]any{"role": msg.Type, "text": strings.TrimSpace(part.Text)})
					}
				}
			}
		}
		if last.Outcome != "" {
			break
		}
		if time.Now().After(deadline) {
			_ = oc.abort(session.ID)
			fail("timeout", fmt.Errorf("job exceeded %s", timeout), true)
			r.cleanupWorktree(spec, worktree, false)
			return
		}
	}

	if last.Outcome != "succeeded" {
		detail := last.Outcome
		if msgs, err := oc.messages(session.ID); err == nil {
			for i := len(msgs) - 1; i >= 0; i-- {
				if msgs[i].Error != nil {
					detail = msgs[i].Error.Type + ": " + msgs[i].Error.Message
					break
				}
			}
		}
		fail("agent_failed", errors.New(detail), true)
		r.cleanupWorktree(spec, worktree, false)
		return
	}

	// The poll loop may finish before any message is visible (fast jobs);
	// pull the transcript once so the hub still records what the agent said.
	if !textEmitted {
		if msgs, err := oc.messages(session.ID); err == nil {
			for _, msg := range msgs {
				for _, part := range msg.Content {
					if part.Type == "text" && strings.TrimSpace(part.Text) != "" {
						emit("log", map[string]any{"role": msg.Type, "text": strings.TrimSpace(part.Text)})
					}
				}
			}
		}
	}

	// 4. Result.
	branch, commit, diffstat := gitResult(worktree, baseSHA)
	env, _ := frame("job.result", JobResult{
		JobID:    spec.JobID,
		Branch:   branch,
		Commit:   commit,
		Diffstat: diffstat,
		Summary:  last.Title,
		ExitCode: 0,
	})
	_ = r.send(env)
	r.log.Printf("[job %s] done: %s %s", spec.JobID, branch, commit)
	emit("status", map[string]any{"phase": "done", "commit": commit, "tokens": last.Tokens, "cost": last.Cost})
	r.cleanupWorktree(spec, worktree, true)
}

func (r *runner) emit(jobID, kind string, data any) {
	env, err := frame("job.event", JobEvent{JobID: jobID, Seq: 0, Kind: kind, Data: data})
	if err != nil {
		return
	}
	_ = r.send(env)
}

// prepareWorktree makes sure the repo exists and cuts a per-job worktree so the
// user's own checkout is never touched.
func (r *runner) prepareWorktree(ctx context.Context, spec JobSpec) (worktree, baseSHA string, err error) {
	repoDir := expandHome(spec.Repo.Dir)
	if repoDir == "" {
		return "", "", errors.New("job has no repo.dir")
	}
	if _, err := os.Stat(filepath.Join(repoDir, ".git")); err != nil {
		if spec.Repo.GitURL == "" {
			return "", "", fmt.Errorf("%s is not a git repository and no gitUrl was given", repoDir)
		}
		if out, err := run("", nil, "git", "clone", spec.Repo.GitURL, repoDir); err != nil {
			return "", "", fmt.Errorf("git clone: %v: %s", err, strings.TrimSpace(out))
		}
	}
	if out, err := run(repoDir, nil, "git", "fetch", "--all", "--prune"); err != nil {
		r.log.Printf("[warn] git fetch: %v: %s", err, strings.TrimSpace(out))
	}
	worktree = spec.Worktree.Dir
	if worktree == "" {
		worktree = filepath.Join(repoDir, ".dsh", "worktrees", spec.JobID)
	}
	if err := os.MkdirAll(filepath.Dir(worktree), 0o755); err != nil {
		return "", "", err
	}
	branch := spec.Branch
	if branch == "" {
		branch = "feat/" + spec.Ticket
	}
	base := spec.Base
	if base == "" {
		base = "HEAD"
	}
	// Resolve the base before cutting the worktree: "HEAD" inside the new
	// worktree is the branch tip, so diffing against it would always be empty.
	resolved, err := run(repoDir, nil, "git", "rev-parse", base)
	if err != nil {
		return "", "", fmt.Errorf("git rev-parse %s: %v", base, err)
	}
	baseSHA = strings.TrimSpace(resolved)
	if out, err := run(repoDir, nil, "git", "worktree", "add", "-B", branch, worktree, baseSHA); err != nil {
		return "", "", fmt.Errorf("git worktree add: %v: %s", err, strings.TrimSpace(out))
	}
	if spec.GitUser != "" {
		_, _ = run(worktree, nil, "git", "config", "user.name", spec.GitUser)
	}
	if spec.GitEmail != "" {
		_, _ = run(worktree, nil, "git", "config", "user.email", spec.GitEmail)
	}
	return worktree, baseSHA, nil
}

func (r *runner) cleanupWorktree(spec JobSpec, worktree string, success bool) {
	if !success {
		r.log.Printf("[job %s] kept worktree for inspection: %s", spec.JobID, worktree)
		return
	}
	repoDir := expandHome(spec.Repo.Dir)
	if out, err := run(repoDir, nil, "git", "worktree", "remove", "--force", worktree); err != nil {
		r.log.Printf("[warn] worktree remove: %v %s", err, strings.TrimSpace(out))
	}
}

// gitResult reads the branch, commit and diffstat the agent produced.
func gitResult(worktree, base string) (branch, commit, diffstat string) {
	if out, err := run(worktree, nil, "git", "rev-parse", "--abbrev-ref", "HEAD"); err == nil {
		branch = strings.TrimSpace(out)
	}
	if out, err := run(worktree, nil, "git", "rev-parse", "HEAD"); err == nil {
		commit = strings.TrimSpace(out)
	}
	rangeSpec := "HEAD"
	if base != "" {
		rangeSpec = base + "...HEAD"
	}
	if out, err := run(worktree, nil, "git", "diff", "--stat", rangeSpec); err == nil {
		diffstat = strings.TrimSpace(out)
	}
	return branch, commit, diffstat
}
