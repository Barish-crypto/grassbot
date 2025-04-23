def main():
    with open('proxy.txt', 'r') as f_in:
        lines = f_in.readlines()
    
    with open('proxy.txt', 'w') as f_out:
        for line in lines:
            if not line.startswith('http://'):
                line = 'http://' + line
            f_out.write(line)

if __name__ == '__main__':
    main()

