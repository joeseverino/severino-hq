package mask

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strconv"
	"strings"
	"testing"
)

// carried is a secret in each form it is checked in: the text that carries
// it, and the part of that form that must not survive. A base64 run keeps
// its first and last two characters, which it can share with its neighbors.
func carried(secret string) map[string][2]string {
	quoted, _ := json.Marshal(secret)
	inner := func(encoded string) string { return encoded[2 : len(encoded)-2] }
	std, web := base64.StdEncoding.EncodeToString, base64.URLEncoding.EncodeToString
	basic := std([]byte("reader:" + secret))
	shifted := std([]byte("ab" + secret + "tail"))
	return map[string][2]string{
		"plain":               {"refused: " + secret + " was not accepted", secret},
		"go quoted":           {"refused: " + strconv.Quote(secret), strconv.Quote(secret)[1 : len(strconv.Quote(secret))-1]},
		"json":                {`{"value":` + string(quoted) + `}`, string(quoted[1 : len(quoted)-1])},
		"url query":           {"GET /api?token=" + url.QueryEscape(secret) + "&page=2", url.QueryEscape(secret)},
		"url path":            {"GET /api/" + url.PathEscape(secret) + "/items", url.PathEscape(secret)},
		"base64":              {"Authorization: Bearer " + std([]byte(secret)), inner(std([]byte(secret)))},
		"base64 url alphabet": {"state=" + web([]byte(secret)), inner(web([]byte(secret)))},
		"base64 basic":        {"Authorization: Basic " + basic, inner(basic[len(std([]byte("reader:"))):])},
		"base64 offset two":   {shifted, inner(shifted[4 : len(shifted)-8])},
	}
}

// secrets holds the characters each encoding treats differently: space,
// quotes, a slash, a plus, an ampersand and a character outside ASCII.
var secrets = []string{
	"sentinel-mask-plain-0123456789",
	`sentinel "quoted" pass/word+&é`,
}

func TestEveryFormOfALearnedValueIsStruck(t *testing.T) {
	Learn(secrets...)
	for _, secret := range secrets {
		for name, form := range carried(secret) {
			text, leak := form[0], form[1]
			if struck := Text(text); strings.Contains(struck, leak) || !strings.Contains(struck, Redacted) {
				t.Errorf("%q, %s: %q survives in %q", secret, name, leak, struck)
			}
		}
	}
}

func TestAShortValueIsNotLearned(t *testing.T) {
	Learn("ab", "")
	if got := Text("tab and lab"); got != "tab and lab" {
		t.Fatalf("a two-character value was struck: %q", got)
	}
}

func TestTheLoggerStrikesEveryPartOfARecord(t *testing.T) {
	secret := "sentinel-mask-logger-0123456789"
	var out bytes.Buffer
	log := Logger(&out)
	// Formatted before the value is learned: the handler keeps these bytes.
	early := log.With("early", secret)
	Learn(secret)
	err := fmt.Errorf("provider refused: %w", errors.New("token "+secret+" expired"))
	early.Warn("step failed: "+secret,
		slog.Any("error", err),
		slog.String("said", `"`+secret+`"`),
		slog.Group(secret, slog.String(secret, "x")),
	)
	if line := out.String(); strings.Contains(line, secret) || !strings.Contains(line, Redacted) {
		t.Fatalf("the log line carries the secret: %s", line)
	}
}

func TestAWriteReportsWhatItWasGiven(t *testing.T) {
	secret := "sentinel-mask-writer-0123456789"
	Learn(secret)
	var out bytes.Buffer
	given := []byte("a " + secret + "\n")
	n, err := Writer(&out).Write(given)
	if err != nil || n != len(given) {
		t.Fatalf("wrote %d of %d: %v", n, len(given), err)
	}
	if strings.Contains(out.String(), secret) {
		t.Fatalf("written through: %q", out.String())
	}
}
