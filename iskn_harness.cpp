// SPDX-License-Identifier: MIT
/*
 * Minimal libISKN_API.so harness.
 *
 * This intentionally avoids vendor headers. It calls the exported C++ symbols
 * through dlsym so we can run the real library under LD_PRELOAD tracing and
 * capture its serial protocol.
 */

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <iostream>
#include <thread>

namespace {

template <typename T>
T load_symbol(void *lib, const char *name)
{
	void *sym = dlsym(lib, name);
	if (!sym) {
		std::cerr << "missing symbol " << name << ": " << dlerror() << "\n";
		std::exit(1);
	}
	return reinterpret_cast<T>(sym);
}

} // namespace

int main(int argc, char **argv)
{
	const char *lib_path = "./libISKN_API.so.1.0.0";
	int max_id = 32;

	if (argc > 1)
		max_id = std::atoi(argv[1]);

	void *lib = dlopen(lib_path, RTLD_NOW);
	if (!lib) {
		std::cerr << "dlopen " << lib_path << ": " << dlerror() << "\n";
		return 1;
	}

	using ctor_t = void (*)(void *, int);
	using dtor_t = void (*)(void *);
	using connect_t = bool (*)(void *);
	using disconnect_t = bool (*)(void *);
	using request_t = bool (*)(void *, int);
	using subscribe_t = bool (*)(void *, int);

	auto slate_ctor = load_symbol<ctor_t>(
		lib, "_ZN8ISKN_API12SlateManagerC1ENS_13CommLayerTypeE");
	auto slate_dtor = load_symbol<dtor_t>(
		lib, "_ZN8ISKN_API12SlateManagerD1Ev");
	auto connect = load_symbol<connect_t>(
		lib, "_ZN8ISKN_API12SlateManager7connectEv");
	auto disconnect = load_symbol<disconnect_t>(
		lib, "_ZN8ISKN_API12SlateManager10disconnectEv");
	auto request = load_symbol<request_t>(
		lib, "_ZN8ISKN_API12SlateManager7requestENS_22SingleRequestBlockTypeE");
	auto subscribe = load_symbol<subscribe_t>(
		lib, "_ZN8ISKN_API12SlateManager9subscribeENS_13AutoBlockTypeE");

	alignas(16) unsigned char manager[256];
	std::memset(manager, 0, sizeof(manager));

	// CommLayerType 0 constructs the USB/serial layer in this library.
	slate_ctor(manager, 0);

	bool connected = connect(manager);
	std::cerr << "connect=" << connected << "\n";
	if (!connected) {
		slate_dtor(manager);
		return 2;
	}

	for (int id = 0; id <= max_id; ++id) {
		bool ok = request(manager, id);
		std::cerr << "request " << id << " -> " << ok << "\n";
		std::this_thread::sleep_for(std::chrono::milliseconds(50));
	}

	for (int id = 0; id <= max_id; ++id) {
		bool ok = subscribe(manager, id);
		std::cerr << "subscribe " << id << " -> " << ok << "\n";
		std::this_thread::sleep_for(std::chrono::milliseconds(50));
	}

	std::cerr << "listening; move pen\n";
	std::this_thread::sleep_for(std::chrono::seconds(10));

	disconnect(manager);
	slate_dtor(manager);
	return 0;
}
