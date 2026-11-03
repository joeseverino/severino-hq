package secrets

import (
	"bytes"
	"encoding/base64"
	"net/url"
	"strconv"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
	"github.com/joeseverino/severino-hq/controller/secrets/connecttest"
)

// What the renderer reads as a secret (its Connect token, a concealed field,
// an application variable) never reaches its log, in any form a message
// might carry it in.
func TestWhatTheRendererReadsNeverReachesItsLog(t *testing.T) {
	h := newHost(t, apiToken(1, "example", "EXAMPLE"))
	var log bytes.Buffer
	h.runner.Log = mask.Logger(&log)
	h.ok()
	for _, secret := range []string{connecttest.Token, secretToken, secretEnv} {
		encoded := base64.StdEncoding.EncodeToString([]byte(secret))
		quoted := strconv.Quote(secret)
		for name, form := range map[string][2]string{
			"plain":        {"refused " + secret, secret},
			"base64":       {"refused Basic " + encoded, encoded[2 : len(encoded)-2]},
			"url-encoded":  {"refused /v1?token=" + url.QueryEscape(secret), url.QueryEscape(secret)},
			"quote-escape": {"refused " + quoted, quoted[1 : len(quoted)-1]},
		} {
			log.Reset()
			h.runner.Log.Error(form[0])
			if strings.Contains(log.String(), form[1]) {
				t.Errorf("%s %s reaches the log: %s", secret[:12], name, log.String())
			}
		}
	}
}
