package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func main() {
	log.SetFlags(0)
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "version", "--version", "-v":
		fmt.Printf("dsh-runner %s (%s/%s, protocol %d)\n", Version, runtime.GOOS, runtime.GOARCH, ProtocolVersion)
	case "login":
		err = cmdLogin(os.Args[2:])
	case "start":
		err = cmdStart(os.Args[2:])
	case "status":
		err = cmdStatus(os.Args[2:])
	case "doctor":
		err = cmdDoctor(os.Args[2:])
	case "keys":
		err = cmdKeys(os.Args[2:])
	case "verify":
		err = cmdVerify(os.Args[2:])
	case "help", "--help", "-h":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Print(`dsh-runner — local execution agent for the superagent platform

usage:
  dsh-runner login  --server URL [--token TOKEN | --oauth]
  dsh-runner start  [--server URL]
  dsh-runner status
  dsh-runner doctor
  dsh-runner keys generate --out KEYFILE
  dsh-runner verify --pub KEY --file FILE --sig FILE.sig
  dsh-runner version

env:
  DSH_RUNNER_HOME      state dir (default ~/.dsh-runner)
`)
}

// --- login -----------------------------------------------------------------

type registerRequest struct {
	AccessToken     string `json:"accessToken"`
	DeviceID        string `json:"deviceId"`
	DevicePublicKey string `json:"devicePublicKey"`
	OS              string `json:"os"`
	Arch            string `json:"arch"`
	Version         string `json:"version"`
}

type registerResponse struct {
	DeviceID    string `json:"deviceId"`
	DeviceToken string `json:"deviceToken"`
	Server      string `json:"server"`
}

func cmdLogin(args []string) error {
	fs := flag.NewFlagSet("login", flag.ExitOnError)
	server := fs.String("server", "", "hub base URL, e.g. http://127.0.0.1:4096")
	token := fs.String("token", "", "dev/CI token (skips the browser OAuth flow)")
	oauth := fs.Bool("oauth", false, "run the OAuth 2.0 PKCE flow in a browser")
	_ = fs.Parse(args)

	cfg, err := loadOrCreateConfig()
	if err != nil {
		return err
	}
	if *server != "" {
		cfg.Server = strings.TrimRight(*server, "/")
	}
	if cfg.Server == "" {
		return errors.New("--server is required the first time")
	}
	if *token == "" && !*oauth {
		return errors.New("pass --token for dev/CI, or --oauth for the browser flow")
	}

	var reg registerResponse
	if *token != "" {
		reg, err = registerDevice(cfg, *token)
		if err != nil {
			return err
		}
	} else {
		reg, err = oauthLogin(cfg)
		if err != nil {
			return err
		}
	}
	cfg.DeviceID = reg.DeviceID
	cfg.DeviceToken = reg.DeviceToken
	if reg.Server != "" {
		cfg.Server = reg.Server
	}
	if err := saveConfig(cfg); err != nil {
		return err
	}
	fmt.Printf("logged in: device %s at %s\n", cfg.DeviceID, cfg.Server)
	return nil
}

func registerDevice(cfg *Config, accessToken string) (registerResponse, error) {
	body, _ := json.Marshal(registerRequest{
		AccessToken:     accessToken,
		DeviceID:        cfg.DeviceID,
		DevicePublicKey: cfg.DevicePublicKey,
		OS:              runtime.GOOS,
		Arch:            runtime.GOARCH,
		Version:         Version,
	})
	res, err := http.Post(cfg.Server+"/runner/register", "application/json", bytes.NewReader(body))
	if err != nil {
		return registerResponse{}, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	if res.StatusCode != http.StatusOK {
		return registerResponse{}, fmt.Errorf("register failed: %d %s", res.StatusCode, strings.TrimSpace(string(raw)))
	}
	var out registerResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return registerResponse{}, err
	}
	if out.DeviceToken == "" {
		return registerResponse{}, errors.New("hub returned no deviceToken")
	}
	return out, nil
}

// oauthLogin implements Authorization Code + PKCE with a loopback callback.
// The hub owns the SSO wiring; this side only starts the browser and receives
// the one-time code.
func oauthLogin(cfg *Config) (registerResponse, error) {
	verifier, err := randomB64(32)
	if err != nil {
		return registerResponse{}, err
	}
	sum := sha256.Sum256([]byte(verifier))
	challenge := base64.RawURLEncoding.EncodeToString(sum[:])

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return registerResponse{}, err
	}
	defer listener.Close()
	redirect := fmt.Sprintf("http://127.0.0.1:%d/callback", listener.Addr().(*net.TCPAddr).Port)
	authURL := fmt.Sprintf("%s/runner/oauth/start?redirect_uri=%s&code_challenge=%s&device_id=%s",
		cfg.Server, url.QueryEscape(redirect), url.QueryEscape(challenge), url.QueryEscape(cfg.DeviceID))
	fmt.Printf("open this URL to sign in:\n  %s\n", authURL)

	codeCh := make(chan string, 1)
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/callback" {
			http.NotFound(w, r)
			return
		}
		code := r.URL.Query().Get("code")
		if code == "" {
			http.Error(w, "missing code", http.StatusBadRequest)
			return
		}
		codeCh <- code
		fmt.Fprintln(w, "dsh-runner: signed in, you can close this tab.")
	})}
	go func() { _ = srv.Serve(listener) }()
	defer srv.Close()

	var code string
	select {
	case code = <-codeCh:
	case <-time.After(5 * time.Minute):
		return registerResponse{}, errors.New("timed out waiting for the OAuth callback")
	}
	body, _ := json.Marshal(map[string]string{
		"code": code, "codeVerifier": verifier, "deviceId": cfg.DeviceID,
		"devicePublicKey": cfg.DevicePublicKey, "redirectUri": redirect,
	})
	res, err := http.Post(cfg.Server+"/runner/oauth/exchange", "application/json", bytes.NewReader(body))
	if err != nil {
		return registerResponse{}, err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	if res.StatusCode != http.StatusOK {
		return registerResponse{}, fmt.Errorf("oauth exchange failed: %d %s", res.StatusCode, strings.TrimSpace(string(raw)))
	}
	var out registerResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return registerResponse{}, err
	}
	return out, nil
}

func randomB64(n int) (string, error) {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}

// --- start -----------------------------------------------------------------

func cmdStart(args []string) error {
	fs := flag.NewFlagSet("start", flag.ExitOnError)
	server := fs.String("server", "", "override the saved hub URL")
	_ = fs.Parse(args)

	cfg, err := loadOrCreateConfig()
	if err != nil {
		return err
	}
	if *server != "" {
		cfg.Server = strings.TrimRight(*server, "/")
	}
	if cfg.Server == "" || cfg.DeviceToken == "" {
		return errors.New("not logged in: run `dsh-runner login --server <url> --token <token>` first")
	}

	applyEnv(cfg)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	r := newRunner(cfg, logger())
	r.serve(ctx)
	fmt.Println("dsh-runner stopped")
	return nil
}

// --- status / doctor --------------------------------------------------------

func cmdStatus(args []string) error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	applyEnv(cfg)
	fmt.Printf("state dir:   %s\n", stateDir())
	fmt.Printf("server:      %s\n", cfg.Server)
	fmt.Printf("device:      %s (token %s)\n", cfg.DeviceID, mask(cfg.DeviceToken))
	fmt.Printf("workspace:   %s\n", cfg.WorkspaceRoot)
	fmt.Printf("opencode:    %s\n", cfg.OpencodeURL)
	if cfg.Server != "" && cfg.DeviceToken != "" {
		req, _ := http.NewRequest("GET", cfg.Server+"/health", nil)
		req.Header.Set("authorization", "Bearer "+cfg.DeviceToken)
		if res, err := (&http.Client{Timeout: 5 * time.Second}).Do(req); err == nil {
			defer res.Body.Close()
			raw, _ := io.ReadAll(io.LimitReader(res.Body, 1<<16))
			fmt.Printf("hub:         %s %s\n", res.Status, strings.TrimSpace(string(raw)))
		} else {
			fmt.Printf("hub:         unreachable (%v)\n", err)
		}
	}
	return nil
}

func cmdDoctor(args []string) error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	applyEnv(cfg)
	ok := true
	for _, bin := range []string{"git", "opencode"} {
		if path, err := exec.LookPath(bin); err != nil {
			fmt.Printf("✗ %s not on PATH\n", bin)
			ok = false
		} else {
			fmt.Printf("✓ %s %s\n", bin, path)
		}
	}
	if info, err := newOpencode(cfg.OpencodeURL, cfg.OpencodePassword).info(); err != nil {
		fmt.Printf("✗ opencode server at %s: %v\n", cfg.OpencodeURL, err)
		ok = false
	} else {
		fmt.Printf("✓ opencode server %s (v%s, pid %d)\n", cfg.OpencodeURL, info.Version, info.PID)
	}
	if cfg.DeviceToken == "" {
		fmt.Println("✗ not logged in")
		ok = false
	} else {
		fmt.Printf("✓ device %s\n", cfg.DeviceID)
	}
	if !ok {
		return errors.New("doctor found problems")
	}
	fmt.Println("all checks passed")
	return nil
}

// applyEnv lets a supervisor inject local-only settings without editing the
// config file (the opencode password never has to be written to disk).
func applyEnv(cfg *Config) {
	if v := os.Getenv("DSH_OPENCODE_URL"); v != "" {
		cfg.OpencodeURL = v
	}
	if v := os.Getenv("DSH_OPENCODE_PASSWORD"); v != "" {
		cfg.OpencodePassword = v
	}
	if v := os.Getenv("DSH_RUNNER_WORKSPACE"); v != "" {
		cfg.WorkspaceRoot = v
	}
	if v := os.Getenv("DSH_RUNNER_MAX_CONCURRENCY"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.MaxConcurrency = n
		}
	}
}

func mask(s string) string {
	if len(s) <= 8 {
		return "***"
	}
	return s[:4] + "…" + s[len(s)-4:]
}

// --- keys ------------------------------------------------------------------

func cmdKeys(args []string) error {
	if len(args) == 0 || args[0] != "generate" {
		return errors.New("usage: dsh-runner keys generate --out KEYFILE")
	}
	fs := flag.NewFlagSet("keys generate", flag.ExitOnError)
	out := fs.String("out", "runner-key.txt", "key file")
	_ = fs.Parse(args[1:])
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	line := "pub:" + base64.StdEncoding.EncodeToString(pub) + "\n" +
		"priv:" + base64.StdEncoding.EncodeToString(priv.Seed()) + "\n"
	if err := os.WriteFile(*out, []byte(line), 0o600); err != nil {
		return err
	}
	fmt.Printf("wrote %s (public key %s)\n", *out, base64.StdEncoding.EncodeToString(pub))
	return nil
}

// cmdVerify checks an ed25519 signature over a release artifact.
func cmdVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	pubB64 := fs.String("pub", "", "base64 ed25519 public key")
	file := fs.String("file", "", "artifact")
	sigFile := fs.String("sig", "", "signature file (base64 or raw)")
	_ = fs.Parse(args)
	if *pubB64 == "" || *file == "" || *sigFile == "" {
		return errors.New("usage: dsh-runner verify --pub KEY --file FILE --sig FILE.sig")
	}
	pub, err := base64.StdEncoding.DecodeString(*pubB64)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return errors.New("bad public key")
	}
	payload, err := os.ReadFile(*file)
	if err != nil {
		return err
	}
	sigRaw, err := os.ReadFile(*sigFile)
	if err != nil {
		return err
	}
	sig, err := decodeSig(strings.TrimSpace(string(sigRaw)))
	if err != nil {
		return err
	}
	if !ed25519.Verify(ed25519.PublicKey(pub), payload, sig) {
		return errors.New("signature mismatch")
	}
	fmt.Printf("ok: %s verified\n", *file)
	return nil
}

func decodeSig(text string) ([]byte, error) {
	if buf, err := base64.StdEncoding.DecodeString(text); err == nil && len(buf) == ed25519.SignatureSize {
		return buf, nil
	}
	if len(text) == ed25519.SignatureSize {
		return []byte(text), nil
	}
	return nil, fmt.Errorf("signature must be %d base64 bytes", ed25519.SignatureSize)
}
