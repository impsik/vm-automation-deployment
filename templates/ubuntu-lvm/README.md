# Ubuntu LVM templates

Run `./install.sh`, select `local_qemu`, choose a data directory and Ubuntu
version, and accept building the image if it is missing. No HCL edits are needed.
Images are installed at `<data-directory>/templates/ubuntu-24.04-lvm/ubuntu-24.04-lvm.qcow2`
(or the corresponding `22.04` path). The default data directory is
`/var/lib/vm-foundry`. Existing images are reused; failed builds remain in a
separate `.build-*` directory for inspection.

The guest uses an LVM root filesystem, cloud-init and the QEMU guest agent.
For a manual build from the repository root, select one file explicitly:

```bash
packer init templates/ubuntu-lvm/ubuntu-24.04-lvm.pkr.hcl
packer build -var 'output_directory=/path/to/output/ubuntu-24.04-lvm' \
  templates/ubuntu-lvm/ubuntu-24.04-lvm.pkr.hcl
```

Without the variable, output is `output/ubuntu-24.04-lvm` relative to the current
directory. Substitute `22.04` to build that version. Do not run `packer build .`
in this directory: the two files are independent templates with identical HCL
block names. Cloud-init for provisioned VMs lives separately in
`templates/cloud-init/user-data.yml`; `http/` is for building the base image only.

Reference: [Packer QEMU builder](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu).
