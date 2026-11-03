package upgrade

import (
	"testing"

	"github.com/joeseverino/severino-hq/controller/host/hosttest"
)

func TestAnUpgradeCommandReturnsPastAnOrphanHoldingItsPipe(t *testing.T) {
	t.Parallel()
	for name, caller := range map[string]string{"through sudo": "1000", "as root": ""} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			u, err := FromEnvironment(func(name string) string {
				if name == "SUDO_UID" {
					return caller
				}
				return ""
			}, 0, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			hosttest.ReturnsPastAnOrphan(t, u.Command)
		})
	}
}
