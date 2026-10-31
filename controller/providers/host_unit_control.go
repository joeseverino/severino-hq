package providers

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/host/unit"
)

// unitAnswerLimit bounds the one line hq-host unit answers with.
const unitAnswerLimit = 4 << 10

// unitWait bounds one request: systemd's own stop and start timeouts, for
// a unit given its default ninety seconds each way.
const unitWait = 4 * time.Minute

// unitSocket reaches `hq-host unit --serve` on the machine this controller
// runs on, through the socket the launcher mounts (Env.UnitSocket). A
// machine reached over SSH would be a second carrier of the same request
// line; until one exists, every other machine is out of reach.
type unitSocket struct {
	path, machine string
	dial          func(ctx context.Context, path string) (net.Conn, error)
}

func (r *Registry) unitControl() UnitControl {
	if r.Env.UnitSocket == "" {
		return nil
	}
	return unitSocket{path: r.Env.UnitSocket, machine: r.controllerID(), dial: dialUnix}
}

func dialUnix(ctx context.Context, path string) (net.Conn, error) {
	var dialer net.Dialer
	return dialer.DialContext(ctx, "unix", path)
}

func (s unitSocket) Reaches(host string) bool { return host != "" && host == s.machine }

// Act sends one request and reads its answer; anything but done is an error
// carrying hq-host's reason.
func (s unitSocket) Act(ctx context.Context, _, verb, name string) error {
	ctx, cancel := context.WithTimeout(ctx, unitWait)
	defer cancel()
	conn, err := s.dial(ctx, s.path)
	if err != nil {
		return &ProviderError{Message: "the unit socket did not answer", Err: err}
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	if _, err := io.WriteString(conn, unit.Request(verb, name)+"\n"); err != nil {
		return &ProviderError{Message: "the unit socket did not take the request", Err: err}
	}
	answer, err := io.ReadAll(io.LimitReader(conn, unitAnswerLimit+1))
	if err != nil || len(answer) > unitAnswerLimit {
		return &ProviderError{Message: "the unit socket's answer did not read", Err: err}
	}
	var result unit.Result
	decoder := json.NewDecoder(strings.NewReader(string(answer)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&result); err != nil {
		return &ProviderError{Message: "the unit socket's answer is not a result", Err: err}
	}
	if result.Outcome != unit.Done {
		return &ProviderError{Message: verb + " " + name + " " + result.Outcome + ": " + result.Reason}
	}
	return nil
}
