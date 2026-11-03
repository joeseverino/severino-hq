package host

import (
	"testing"

	"github.com/joeseverino/severino-hq/controller/host/hosttest"
)

func TestACommandReturnsPastAnOrphanHoldingItsPipe(t *testing.T) {
	t.Parallel()
	hosttest.ReturnsPastAnOrphan(t, Command)
}
