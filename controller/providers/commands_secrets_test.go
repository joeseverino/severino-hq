package providers

import (
	"bytes"
	"context"
	"encoding/base64"
	"net/url"
	"strconv"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/mask"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A child that prints a secret it was handed, by its environment, its input
// or its arguments, in any of the forms it may print it, fails without the
// secret reaching the controller's log or the error it reports.
func TestAChildsStderrNeverCarriesItsSecret(t *testing.T) {
	secret := `sentinel "child" token/0123+&`
	held := runtime.NewConnections(connections.Connection{Ref: "example", Provider: "example", ServiceAccount: &connections.ServiceAccount{APIToken: secret}})
	account, err := runtime.Need(held.All()[0].ServiceAccount)
	if err != nil {
		t.Fatal(err)
	}
	encoded := base64.StdEncoding.EncodeToString([]byte(account.APIToken))
	quoted := strconv.Quote(account.APIToken)
	for name, said := range map[string][2]string{
		"plain":        {"error: token " + secret + " refused", secret},
		"base64":       {"error: Basic " + encoded, encoded[2 : len(encoded)-2]},
		"url-encoded":  {"error: GET /v1?token=" + url.QueryEscape(secret), url.QueryEscape(secret)},
		"quote-escape": {"error: token " + quoted, quoted[1 : len(quoted)-1]},
	} {
		t.Run(name, func(t *testing.T) {
			var log bytes.Buffer
			commands := &Commands{Supplied: held, Log: mask.Logger(&log), Exec: func(context.Context, []string, []byte, []string) ([]byte, []byte, int, error) {
				return nil, []byte("starting\n" + said[0] + "\n"), 1, nil
			}}
			_, err := commands.Run(t.Context(), []string{"example", account.APIToken}, []byte(account.APIToken), "example step", "", map[string]string{"EXAMPLE_TOKEN": account.APIToken})
			if err == nil {
				t.Fatal("a child that exited 1 did not fail")
			}
			if !strings.Contains(log.String(), "error:") {
				t.Fatalf("the child's last line was not logged: %s", log.String())
			}
			for where, text := range map[string]string{"log": log.String(), "report": runtime.ReportText(err.Error())} {
				if strings.Contains(text, said[1]) {
					t.Errorf("the %s carries the secret: %s", where, text)
				}
			}
		})
	}
}
