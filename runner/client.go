package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// runner keeps one WSS connection to the hub and executes offered jobs.
type runner struct {
	cfg  *Config
	ws   *websocket.Conn
	mu   sync.Mutex
	jobs map[string]*runningJob
	log  *log.Logger
}

type runningJob struct {
	spec   JobSpec
	cancel context.CancelFunc
}

func newRunner(cfg *Config, logger *log.Logger) *runner {
	return &runner{cfg: cfg, jobs: map[string]*runningJob{}, log: logger}
}

func (r *runner) endpoint() string {
	base := strings.TrimRight(r.cfg.Server, "/")
	if strings.HasPrefix(base, "https://") {
		base = "wss://" + strings.TrimPrefix(base, "https://")
	} else if strings.HasPrefix(base, "http://") {
		base = "ws://" + strings.TrimPrefix(base, "http://")
	}
	return base + "/runner/connect"
}

// serve reconnects forever with exponential backoff, as the hub may restart.
func (r *runner) serve(ctx context.Context) error {
	backoff := time.Second
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		err := r.connectOnce(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		r.log.Printf("[warn] connection ended: %v; reconnecting in %s", err, backoff)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
		if backoff < 60*time.Second {
			backoff *= 2
			if backoff > 60*time.Second {
				backoff = 60 * time.Second
			}
		}
	}
}

func (r *runner) connectOnce(ctx context.Context) error {
	header := http.Header{}
	header.Set("Authorization", "Bearer "+r.cfg.DeviceToken)
	dialer := websocket.Dialer{HandshakeTimeout: 15 * time.Second}
	ws, resp, err := dialer.DialContext(ctx, r.endpoint(), header)
	if err != nil {
		if resp != nil {
			return errors.New(err.Error() + " (status " + resp.Status + ")")
		}
		return err
	}
	defer ws.Close()
	r.mu.Lock()
	r.ws = ws
	r.mu.Unlock()

	hello, _ := frame("hello", Hello{
		DeviceID:       r.cfg.DeviceID,
		Version:        Version,
		OS:             runtime.GOOS,
		Arch:           runtime.GOARCH,
		Capabilities:   []string{"worktree", "cancel", "eventStream"},
		MaxConcurrency: r.cfg.MaxConcurrency,
	})
	if err := r.send(hello); err != nil {
		return err
	}
	r.log.Printf("[connect] %s (%s/%s) as %s", r.endpoint(), runtime.GOOS, runtime.GOARCH, r.cfg.DeviceID)

	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-heartbeat.C:
				hb, _ := frame("heartbeat", Heartbeat{
					RunningJobs: r.runningCount(),
					Version:     Version,
				})
				if err := r.send(hb); err != nil {
					return
				}
			}
		}
	}()

	for {
		_, raw, err := ws.ReadMessage()
		if err != nil {
			return err
		}
		var env Envelope
		if err := json.Unmarshal(raw, &env); err != nil {
			r.log.Printf("[warn] bad frame: %v", err)
			continue
		}
		switch env.Type {
		case "welcome":
			var w Welcome
			_ = json.Unmarshal(env.Payload, &w)
			r.log.Printf("[welcome] protocol=%d heartbeat=%ds lease=%ds", w.Protocol, w.HeartbeatSeconds, w.LeaseSeconds)
		case "job.offer":
			var spec JobSpec
			if err := json.Unmarshal(env.Payload, &spec); err != nil {
				r.log.Printf("[warn] bad job.offer: %v", err)
				continue
			}
			go r.execJob(ctx, spec)
		case "job.cancel":
			var c Cancel
			_ = json.Unmarshal(env.Payload, &c)
			r.mu.Lock()
			job := r.jobs[c.JobID]
			r.mu.Unlock()
			if job != nil && job.cancel != nil {
				r.log.Printf("[cancel] job %s: %s", c.JobID, c.Reason)
				job.cancel()
			}
		case "ping":
			pong, _ := frame("pong", map[string]any{})
			_ = r.send(pong)
		}
	}
}

func (r *runner) send(env Envelope) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.ws == nil {
		return errors.New("not connected")
	}
	return r.ws.WriteJSON(env)
}

func (r *runner) runningCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.jobs)
}

func (r *runner) registerJob(spec JobSpec, cancel context.CancelFunc) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.jobs[spec.JobID] = &runningJob{spec: spec, cancel: cancel}
}

func (r *runner) finishJob(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.jobs, id)
}

// loadOrCreateConfig loads the runner config and makes sure the device key exists.
func loadOrCreateConfig() (*Config, error) {
	cfg, err := loadConfig()
	if err != nil {
		return nil, err
	}
	if cfg.DeviceID == "" {
		cfg.DeviceID = newID("dev")
	}
	if err := ensureDeviceKey(cfg); err != nil {
		return nil, err
	}
	if err := saveConfig(cfg); err != nil {
		return nil, err
	}
	return cfg, nil
}

func logger() *log.Logger {
	return log.New(os.Stderr, "", log.LstdFlags|log.Lmsgprefix)
}
