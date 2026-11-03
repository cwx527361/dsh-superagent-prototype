package main

import (
	"encoding/json"
	"time"
)

// ProtocolVersion is the runner <-> hub wire version.
const ProtocolVersion = 1

// Envelope is every frame in both directions.
type Envelope struct {
	V       int             `json:"v"`
	ID      string          `json:"id,omitempty"`
	Type    string          `json:"type"`
	TS      string          `json:"ts,omitempty"`
	Payload json.RawMessage `json:"payload,omitempty"`
}

func frame(kind string, payload any) (Envelope, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return Envelope{}, err
	}
	return Envelope{V: ProtocolVersion, ID: newID("frame"), Type: kind, TS: nowISO(), Payload: raw}, nil
}

// Hello announces the runner after connecting.
type Hello struct {
	DeviceID       string   `json:"deviceId"`
	Version        string   `json:"version"`
	OS             string   `json:"os"`
	Arch           string   `json:"arch"`
	Capabilities   []string `json:"capabilities"`
	Projects       []string `json:"projects,omitempty"`
	MaxConcurrency int      `json:"maxConcurrency"`
	ResumeJobs     []string `json:"resumeJobs,omitempty"`
}

// Heartbeat keeps the 60s lease alive; the hub marks a job orphaned only after
// two missed periods, not on the first lost heartbeat.
type Heartbeat struct {
	RunningJobs   int    `json:"runningJobs"`
	Load          string `json:"load,omitempty"`
	FreeDiskBytes int64  `json:"freeDiskBytes,omitempty"`
	Version       string `json:"version"`
}

// Welcome is the hub's first frame.
type Welcome struct {
	ServerTime       string `json:"serverTime"`
	Protocol         int    `json:"protocol"`
	HeartbeatSeconds int    `json:"heartbeatSeconds"`
	LeaseSeconds     int    `json:"leaseSeconds"`
	MaxJobSeconds    int    `json:"maxJobSeconds"`
}

// JobSpec is one unit of work from the hub.
type JobSpec struct {
	JobID     string      `json:"jobId"`
	Ticket    string      `json:"ticket"`
	Repo      RepoRef     `json:"repo"`
	Base      string      `json:"base"`
	Branch    string      `json:"branch"`
	Prompt    string      `json:"prompt"`
	Model     string      `json:"model"`
	TimeoutMs int         `json:"timeoutMs"`
	JobToken  string      `json:"jobToken"`
	GitUser   string      `json:"gitUser,omitempty"`
	GitEmail  string      `json:"gitEmail,omitempty"`
	Worktree  WorktreeRef `json:"worktree"`
}

// RepoRef names the repository to work in; Dir is a local path (M1) and GitURL
// is what a fresh machine clones from (M3).
type RepoRef struct {
	Dir    string `json:"dir"`
	GitURL string `json:"gitUrl,omitempty"`
}

// WorktreeRef tells the runner where to cut the worktree; empty means derive it
// from the repo directory.
type WorktreeRef struct {
	Dir string `json:"dir,omitempty"`
}

// JobEvent is streamed while a job runs.
type JobEvent struct {
	JobID string `json:"jobId"`
	Seq   int    `json:"seq"`
	Kind  string `json:"kind"` // status | log | tool | diff
	Data  any    `json:"data"`
}

// JobResult is a successful end.
type JobResult struct {
	JobID    string `json:"jobId"`
	Branch   string `json:"branch"`
	Commit   string `json:"commit"`
	Diffstat string `json:"diffstat"`
	Summary  string `json:"summary"`
	ExitCode int    `json:"exitCode"`
}

// JobFailure ends a job without a result.
type JobFailure struct {
	JobID     string `json:"jobId"`
	Cause     string `json:"cause"`
	Detail    string `json:"detail"`
	Retryable bool   `json:"retryable"`
}

// Cancel asks the runner to stop a job.
type Cancel struct {
	JobID  string `json:"jobId"`
	Reason string `json:"reason"`
}

func nowISO() string { return time.Now().UTC().Format(time.RFC3339) }
