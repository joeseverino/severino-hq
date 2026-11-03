package unit

import (
	"testing"

	"github.com/joeseverino/severino-hq/controller/host/hosttest"
)

func TestARootCommandReturnsPastAnOrphanHoldingItsPipe(t *testing.T) {
	t.Parallel()
	hosttest.ReturnsPastAnOrphan(t, rootCommand)
}
