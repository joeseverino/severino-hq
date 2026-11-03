package host

import (
	"bytes"
	"encoding/base64"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
)

// A token a hand-off carries never reaches hq-host's log, whether the run it
// names is taken or refused.
func TestAHandoffTokenNeverReachesTheLog(t *testing.T) {
	for name, edit := range map[string]func(run, top map[string]any){
		"taken":   nil,
		"refused": func(run, _ map[string]any) { run["owner"] = "not an owner"; run["token"] = exampleToken + "-refused" },
	} {
		t.Run(name, func(t *testing.T) {
			_, _ = ParseHandoff(handoffDocument(t, edit))
			token := exampleToken
			if edit != nil {
				token += "-refused"
			}
			var log bytes.Buffer
			mask.Logger(&log).Warn("hand-off refused: "+token, "said", base64.StdEncoding.EncodeToString([]byte(token)))
			encoded := base64.StdEncoding.EncodeToString([]byte(token))
			if strings.Contains(log.String(), token) || strings.Contains(log.String(), encoded[2:len(encoded)-2]) {
				t.Fatalf("the log carries the token: %s", log.String())
			}
		})
	}
}
