package providers

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

type localFixture struct {
	Provider   string                     `json:"provider"`
	Surface    string                     `json:"surface"`
	Env        map[string]string          `json:"env"`
	Commands   map[string]json.RawMessage `json:"commands"`
	Routes     map[string]json.RawMessage `json:"routes"`
	Failures   map[string]string          `json:"failures"`
	Open       []string                   `json:"open"`
	Containers json.RawMessage            `json:"containers"`
	Firewall   json.RawMessage            `json:"firewall"`
	Plan       runtime.GlancePlan         `json:"plan"`
	Refs       []string                   `json:"refs"`
	Registry   runtime.ControllerRegistry `json:"registry"`
	Carry      []string                   `json:"carry"`
	Resource   runtime.Resource           `json:"resource"`
	Action     string                     `json:"action"`
	Apply      bool                       `json:"apply"`
	Only       []string                   `json:"only"`
	Undeclared []string                   `json:"undeclared"`
	Zones      map[string][]RedirectZone  `json:"zones"`
	Portainer  struct {
		URL          string            `json:"url"`
		Headers      map[string]string `json:"headers"`
		Environments []struct {
			ID        json.RawMessage `json:"id"`
			Name      string          `json:"name"`
			Reachable bool            `json:"reachable"`
			Local     bool            `json:"local"`
		} `json:"environments"`
	} `json:"portainer"`
}

// localRecorder answers and records the commands, HTTP calls and zone reads,
// in order, as the Python harness's LocalFixture does.
type localRecorder struct {
	fixture  localFixture
	mu       sync.Mutex // inventory reads providers concurrently
	requests []Object
	refusals []Object
}

func (l *localRecorder) record(request Object) {
	l.mu.Lock()
	l.requests = append(l.requests, request)
	l.mu.Unlock()
}

func (l *localRecorder) exec(_ context.Context, argv []string, stdin []byte, _ []string) ([]byte, []byte, int, error) {
	var input any
	if len(stdin) > 0 {
		input = string(stdin)
	}
	l.record(Object{"kind": "command", "argv": argv, "input": input})
	ref := ""
	for i, arg := range argv {
		if arg == "-i" && i+1 < len(argv) {
			ref = filepath.Base(argv[i+1])
		}
	}
	answer := l.fixture.Commands[ref+" "+argv[len(argv)-1]]
	if string(answer) == `"missing"` {
		return nil, nil, 0, os.ErrNotExist
	}
	if string(answer) == `"overflow"` {
		return nil, nil, 0, errOutputLimit
	}
	var reply struct {
		Stdout string `json:"stdout"`
		Stderr string `json:"stderr"`
		Exit   int    `json:"exit"`
	}
	if len(answer) > 0 {
		json.Unmarshal(answer, &reply)
	}
	return []byte(reply.Stdout), []byte(reply.Stderr), reply.Exit, nil
}

func (l *localRecorder) Request(_ context.Context, address, method string, _ map[string]string, _ any) (json.RawMessage, error) {
	l.record(Object{"kind": "http", "url": address, "method": method})
	if message, ok := l.fixture.Failures[address]; ok {
		return nil, &ProviderError{Message: message}
	}
	if value, ok := l.fixture.Routes[address]; ok {
		if string(value) == "null" {
			return nil, nil
		}
		return value, nil
	}
	return nil, errors.New("unexpected read: " + address)
}

func (l *localRecorder) RequestHeader(ctx context.Context, address string, headers map[string]string, _ string) (json.RawMessage, string, error) {
	data, err := l.Request(ctx, address, "GET", headers, nil)
	return data, "", err
}

func (l *localRecorder) zoneAnswer(path string) (json.RawMessage, error) {
	if message, ok := l.fixture.Failures[path]; ok {
		return nil, &ProviderError{Message: message}
	}
	if value, ok := l.fixture.Routes[path]; ok {
		return value, nil
	}
	return nil, errors.New("unexpected read: " + path)
}

func (l *localRecorder) Zones(_ context.Context, ref string) ([]RedirectZone, error) {
	return l.fixture.Zones[ref], nil
}

func (l *localRecorder) Listed(_ context.Context, path, _ string) ([]json.RawMessage, error) {
	raw, err := l.zoneAnswer(path)
	if err != nil {
		return nil, err
	}
	listed := []json.RawMessage{}
	if string(raw) != "null" {
		if err := json.Unmarshal(raw, &listed); err != nil {
			return nil, err
		}
	}
	return listed, nil
}

func (l *localRecorder) Result(_ context.Context, path, _ string) (json.RawMessage, error) {
	return l.zoneAnswer(path)
}

func (l *localRecorder) Reason(err error) string { return err.Error() }

func (l *localRecorder) Refuse(_ context.Context, part string, err error, scope, ref string) {
	l.mu.Lock()
	l.refusals = append(l.refusals, Object{"part": part, "reason": err.Error(), "scope": scope, "connection_ref": ref})
	l.mu.Unlock()
}

func (l *localRecorder) URL(string) (string, error) { return l.fixture.Portainer.URL, nil }
func (l *localRecorder) Headers(string) (map[string]string, error) {
	return l.fixture.Portainer.Headers, nil
}
func (l *localRecorder) Environments(context.Context, string) ([]PortainerEnvironment, error) {
	found := []PortainerEnvironment{}
	for _, environment := range l.fixture.Portainer.Environments {
		id, _ := parsePy(environment.ID)
		found = append(found, PortainerEnvironment{ID: id, Name: environment.Name,
			Reachable: environment.Reachable, Local: environment.Local})
	}
	return found, nil
}

// publishedContainers keeps only ports Python's str(port).isdigit() admits.
func (l *localRecorder) publishedContainers(context.Context) ([]PublishedContainer, error) {
	if string(l.fixture.Containers) == `"refused"` {
		return nil, &ProviderError{Message: "Portainer refused."}
	}
	var raw []map[string]json.RawMessage
	if len(l.fixture.Containers) > 0 {
		json.Unmarshal(l.fixture.Containers, &raw)
	}
	found := []PublishedContainer{}
	for _, entry := range raw {
		container := PublishedContainer{}
		json.Unmarshal(entry["host"], &container.Host)
		json.Unmarshal(entry["host_address"], &container.HostAddress)
		var ports []json.RawMessage
		json.Unmarshal(entry["ports"], &ports)
		for _, port := range ports {
			value, err := parsePy(port)
			if err != nil {
				continue
			}
			text := value.str()
			if text == "" || strings.Trim(text, "0123456789") != "" {
				continue
			}
			if n, err := strconv.Atoi(text); err == nil {
				container.Ports = append(container.Ports, n)
			}
		}
		found = append(found, container)
	}
	return found, nil
}

// localParity answers one host-readings, glance or redirects fixture; false when
// the fixture is for another provider.
func localParity(raw []byte) bool {
	var fixture localFixture
	if json.Unmarshal(raw, &fixture) != nil || fixture.Provider != "local" {
		return false
	}
	recorder := &localRecorder{fixture: fixture}
	for _, kind := range fixture.Undeclared {
		delete(fixture.Registry.ConnectionProviders, kind)
	}
	env := runtime.Environment{"HQ_CONTROLLER_SSH_DIR": "/ssh"}
	for name, value := range fixture.Env {
		env[name] = value
	}
	if len(fixture.Firewall) > 0 && string(fixture.Firewall) != "null" {
		file, err := os.CreateTemp("", "parity-firewall-*.json")
		if err == nil {
			file.Write(fixture.Firewall)
			file.Close()
			defer os.Remove(file.Name())
			env["SEVERINO_HOST_FIREWALL"] = file.Name()
		}
	}
	r := New(env, recorder)
	r.Commands.Exec = recorder.exec
	r.ControllerID = "controller"
	r.Portainer = recorder
	r.PublishedContainers = recorder.publishedContainers
	opened := map[string]bool{}
	for _, address := range fixture.Open {
		opened[address] = true
	}
	r.Dial = func(_ context.Context, address string, port int) bool {
		return opened[address+":"+strconv.Itoa(port)]
	}
	ctx := context.Background()
	var value any
	var err error
	switch fixture.Surface {
	case "firewall":
		value, err = r.hostFirewall(ctx)
	case "perimeter":
		value, err = r.hostPerimeter(ctx)
	case "glance":
		value, err = r.Glance(ctx, fixture.Plan)
	case "redirects":
		value, err = ReadRedirects(ctx, fixture.Refs, recorder)
	case "connections":
		value, err = NewController(r, fixture.Registry).Connections(ctx, fixture.Carry)
	case "execute":
		value, err = NewController(r, fixture.Registry).Execute(ctx, fixture.Resource, fixture.Action, fixture.Apply)
	case "inventory":
		value, err = NewController(r, fixture.Registry).Inventory(ctx, fixture.Only)
	default:
		os.Exit(2)
	}
	output := Object{"result": value, "error": "", "requests": recorder.requests, "refusals": recorder.refusals,
		"steps": r.Commands.StepFailures()}
	if recorder.requests == nil {
		output["requests"] = []Object{}
	}
	if recorder.refusals == nil {
		output["refusals"] = []Object{}
	}
	if err != nil {
		output["result"] = nil
		output["error"] = err.Error()
	}
	if json.NewEncoder(os.Stdout).Encode(output) != nil {
		os.Exit(2)
	}
	return true
}
