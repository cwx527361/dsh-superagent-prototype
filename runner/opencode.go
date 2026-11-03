package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// opencode is the local execution kernel; the runner only speaks its HTTP API.
// POC evidence: docker/poc/README.md (session + prompt + transcript against 2.1.2).
type opencode struct {
	base     string
	password string
	hc       *http.Client
}

func newOpencode(base, password string) *opencode {
	return &opencode{
		base:     strings.TrimRight(base, "/"),
		password: password,
		hc:       &http.Client{Timeout: 30 * time.Second},
	}
}

func (o *opencode) call(method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, o.base+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("content-type", "application/json")
	if o.password != "" {
		req.Header.Set("authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte("opencode:"+o.password)))
	}
	res, err := o.hc.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 8<<20))
	if err != nil {
		return err
	}
	if res.StatusCode < 200 || res.StatusCode > 299 {
		return fmt.Errorf("%s %s -> %d: %s", method, path, res.StatusCode, strings.TrimSpace(string(raw)))
	}
	if out != nil && len(raw) > 0 {
		return json.Unmarshal(raw, out)
	}
	return nil
}

type ocInfo struct {
	Version string `json:"version"`
	PID     int    `json:"pid"`
}

func (o *opencode) info() (ocInfo, error) {
	var out ocInfo
	err := o.call("GET", "/api/info", nil, &out)
	return out, err
}

type ocSession struct {
	ID       string `json:"id"`
	Outcome  string `json:"outcome"`
	Title    string `json:"title"`
	Location struct {
		Directory string `json:"directory"`
	} `json:"location"`
	Model struct {
		ProviderID string `json:"providerID"`
		ID         string `json:"id"`
	} `json:"model"`
	Tokens struct {
		Input  int `json:"input"`
		Output int `json:"output"`
	} `json:"tokens"`
	Cost float64 `json:"cost"`
}

func (o *opencode) createSession(directory, model string) (ocSession, error) {
	body := map[string]any{"location": map[string]any{"directory": directory}}
	if model != "" {
		provider, id, ok := strings.Cut(model, "/")
		if ok && provider != "" && id != "" {
			body["model"] = map[string]any{"providerID": provider, "id": id}
		}
	}
	var out struct {
		Data ocSession `json:"data"`
	}
	err := o.call("POST", "/api/session", body, &out)
	return out.Data, err
}

func (o *opencode) prompt(sessionID, text string) error {
	return o.call("POST", "/api/session/"+sessionID+"/prompt", map[string]any{"text": text}, nil)
}

func (o *opencode) session(sessionID string) (ocSession, error) {
	var out struct {
		Data ocSession `json:"data"`
	}
	err := o.call("GET", "/api/session/"+sessionID, nil, &out)
	return out.Data, err
}

type ocMessage struct {
	ID      string `json:"id"`
	Type    string `json:"type"`
	Outcome string `json:"outcome"`
	Finish  string `json:"finish"`
	Error   *struct {
		Type    string `json:"type"`
		Message string `json:"message"`
	} `json:"error"`
	Content []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
}

func (o *opencode) messages(sessionID string) ([]ocMessage, error) {
	var out struct {
		Data []ocMessage `json:"data"`
	}
	err := o.call("GET", "/api/session/"+sessionID+"/message", nil, &out)
	return out.Data, err
}

// abort asks the agent to stop; best-effort because an idle session may 404.
func (o *opencode) abort(sessionID string) error {
	return o.call("POST", "/api/session/"+sessionID+"/abort", map[string]any{}, nil)
}
