# shellcheck shell=sh
# The repository's address as the image records it: https, whatever the
# remote's scheme, so a checkout cloned over SSH stamps what CI stamps.
source_url() {
    git remote get-url origin | sed -E 's#^git@([^:]+):#https://\1/#; s#\.git$##'
}
