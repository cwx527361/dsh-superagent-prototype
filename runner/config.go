package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Version is set at build time with -ldflags "-X main.Version=...".
var Version = "dev"

// Config is the runner's on-disk state. The token is long-lived but revocable;
// the device key is the runner's identity for signed releases and future mTLS.
type Config struct {
	Server           string `json:"server"`
	DeviceID         string `json:"deviceId"`
	DeviceToken      string `json:"deviceToken"`
	DevicePublicKey  string `json:"devicePublicKey"`
	DevicePrivateKey string `json:"devicePrivateKey"`
	Channel          string `json:"channel"`
	WorkspaceRoot    string `json:"workspaceRoot"`
	OpencodeURL      string `json:"opencodeUrl"`
	OpencodePassword string `json:"opencodePassword"`
	MaxConcurrency   int    `json:"maxConcurrency"`
}

func stateDir() string {
	if dir := os.Getenv("DSH_RUNNER_HOME"); dir != "" {
		return dir
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".dsh-runner"
	}
	return filepath.Join(home, ".dsh-runner")
}

func configPath() string { return filepath.Join(stateDir(), "config.json") }

func loadConfig() (*Config, error) {
	raw, err := os.ReadFile(configPath())
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return &Config{
				Channel:        "stable",
				WorkspaceRoot:  defaultWorkspaceRoot(),
				OpencodeURL:    "http://127.0.0.1:4097",
				MaxConcurrency: 1,
			}, nil
		}
		return nil, err
	}
	var cfg Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", configPath(), err)
	}
	if cfg.Channel == "" {
		cfg.Channel = "stable"
	}
	if cfg.WorkspaceRoot == "" {
		cfg.WorkspaceRoot = defaultWorkspaceRoot()
	}
	if cfg.OpencodeURL == "" {
		cfg.OpencodeURL = "http://127.0.0.1:4097"
	}
	if cfg.MaxConcurrency == 0 {
		cfg.MaxConcurrency = 1
	}
	return &cfg, nil
}

func defaultWorkspaceRoot() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "corp-work"
	}
	return filepath.Join(home, "corp-work")
}

func saveConfig(cfg *Config) error {
	if err := os.MkdirAll(stateDir(), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(configPath(), append(raw, '\n'), 0o600)
}

// ensureDeviceKey creates the ed25519 device identity once.
func ensureDeviceKey(cfg *Config) error {
	if cfg.DevicePublicKey != "" && cfg.DevicePrivateKey != "" {
		return nil
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	cfg.DevicePublicKey = base64.StdEncoding.EncodeToString(pub)
	cfg.DevicePrivateKey = base64.StdEncoding.EncodeToString(priv.Seed())
	return nil
}

// devicePrivateKey decodes the stored seed into a private key.
func devicePrivateKey(cfg *Config) (ed25519.PrivateKey, error) {
	seed, err := base64.StdEncoding.DecodeString(cfg.DevicePrivateKey)
	if err != nil {
		return nil, err
	}
	if len(seed) != ed25519.SeedSize {
		return nil, fmt.Errorf("device private key must be %d bytes, got %d", ed25519.SeedSize, len(seed))
	}
	return ed25519.NewKeyFromSeed(seed), nil
}
