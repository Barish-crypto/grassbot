def read_proxy_file():
    with open('proxy.txt', 'r') as f:
        for line in f:
            yield line.strip()
def save_proxies(proxies):
    with open('proxy.txt', 'w') as f:
        for proxy in proxies:
            if proxy['user'] and proxy['pass']:
                f.write(f"http://{proxy['user']}:{proxy['pass']}@{proxy['host']}:{proxy['port']}\n")
            else:
                f.write(f"{proxy['host']}:{proxy['port']}\n")

    print("Proxies saved to proxy.txt")

def main():
    with open('proxy.txt', 'r') as f:
        proxy_list = f.readlines()

    proxies = parse_proxies(proxy_list)

    save_proxies(proxies)

if __name__ == '__main__':
    main()
