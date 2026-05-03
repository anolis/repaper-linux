#include <linux/module.h>
#define INCLUDE_VERMAGIC
#include <linux/build-salt.h>
#include <linux/elfnote-lto.h>
#include <linux/export-internal.h>
#include <linux/vermagic.h>
#include <linux/compiler.h>

BUILD_SALT;
BUILD_LTO_INFO;

MODULE_INFO(vermagic, VERMAGIC_STRING);
MODULE_INFO(name, KBUILD_MODNAME);

__visible struct module __this_module
__section(".gnu.linkonce.this_module") = {
	.name = KBUILD_MODNAME,
	.init = init_module,
#ifdef CONFIG_MODULE_UNLOAD
	.exit = cleanup_module,
#endif
	.arch = MODULE_ARCH_INIT,
};

#ifdef CONFIG_RETPOLINE
MODULE_INFO(retpoline, "Y");
#endif


static const struct modversion_info ____versions[]
__used __section("__versions") = {
	{ 0xbdfb6dbb, "__fentry__" },
	{ 0xd34882ff, "__hid_register_driver" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0xc74bffa2, "_dev_info" },
	{ 0x39e14741, "hid_open_report" },
	{ 0x5ff316f9, "hid_hw_start" },
	{ 0xb23c1e26, "_dev_err" },
	{ 0x5c2c2475, "hid_unregister_driver" },
	{ 0x160c03af, "module_layout" },
};

MODULE_INFO(depends, "hid");

MODULE_ALIAS("hid:b0003g*v00002C87p00000001");
