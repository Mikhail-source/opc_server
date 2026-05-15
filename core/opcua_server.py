from asyncua import Server, ua
import asyncio
from core.tag_registry import TagRegistry

class OPCUAServer:
    def __init__(self, registry: TagRegistry, url: str = "opc.tcp://0.0.0.0:4840"):
        self.server = Server()
        self.registry = registry
        self.url = url
        self.nodes = {}

    async def start(self):
        await self.server.init()
        await self.server.set_endpoint(self.url)
        await self.server.set_server_name("CustomOPCServer")
        
        # Создаём папку для тегов
        idx = await self.server.register_namespace("CustomTags")
        folder = await self.nodes.get_root().add_folder(idx, "Tags")
        
        for name, tag in self.registry.tags.items():
            node = await folder.add_variable(idx, name, tag.value, ua.VariantType.Double)
            await node.set_writable()
            self.nodes[name] = node

        await self.server.start()

    async def sync_from_registry(self):
        """Периодически обновляет значения в OPC UA из реестра"""
        while True:
            async with self.registry._lock:
                for name, tag in self.registry.tags.items():
                    if name in self.nodes and tag.value is not None:
                        await self.nodes[name].set_value(tag.value)
            await asyncio.sleep(0.5)