package npmapi

import (
	"encoding/json"
	"testing"
)

func TestFlagTakesNPMsTwoSpellings(t *testing.T) {
	cases := []struct {
		raw  string
		want Flag
		ok   bool
	}{
		{"true", true, true}, {"false", false, true}, {"1", true, true}, {"0", false, true},
		{`"1"`, false, false}, {"2", false, false}, {"null", false, false}, {`"yes"`, false, false}, {"[]", false, false},
	}
	for _, c := range cases {
		var got Flag
		err := json.Unmarshal([]byte(c.raw), &got)
		if (err == nil) != c.ok || got != c.want {
			t.Errorf("%s: %v %v", c.raw, got, err)
		}
	}
}

func TestIDIsAWholeNumberOrItsDecimalString(t *testing.T) {
	cases := []struct {
		raw  string
		want ID
		ok   bool
	}{
		{"12", 12, true}, {`"12"`, 12, true}, {"0", 0, true},
		{"1.5", 0, false}, {`"new"`, 0, false}, {"true", 0, false}, {"null", 0, false},
	}
	for _, c := range cases {
		var got ID
		err := json.Unmarshal([]byte(c.raw), &got)
		if (err == nil) != c.ok || got != c.want {
			t.Errorf("%s: %v %v", c.raw, got, err)
		}
	}
}
