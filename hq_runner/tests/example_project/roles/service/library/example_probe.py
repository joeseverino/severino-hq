"""A module of a role's own, which Ansible loads from inside the role whatever
its configuration says: the scan reports the directory."""

from ansible.module_utils.basic import AnsibleModule  # type: ignore[import-not-found]  # ansible-core is the runner group's alone


def main() -> None:
    AnsibleModule(argument_spec={}).exit_json(changed=False)


if __name__ == "__main__":
    main()
