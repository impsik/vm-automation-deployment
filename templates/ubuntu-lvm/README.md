# Ubuntu LVM template

This Packer build installs Ubuntu Server with Subiquity's LVM storage layout.
The resulting qcow2 is a reusable backing image; VM Foundry creates thin
overlays from it, so end-user provisioning does not run the installer.

```bash
cd templates/ubuntu-lvm
packer init .
packer build .
```

The default output is:

```text
/home/imre/chia/Hetznerist/kvm/templates/ubuntu-24.04-lvm/ubuntu-24.04-lvm.qcow2
```
