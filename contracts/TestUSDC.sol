// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * TestUSDC — token ERC-20 de PRUEBA para BNB Smart Chain testnet.
 *
 * ⚠️ SOLO PARA TESTNET. No tiene ningun valor real y no debe desplegarse en
 *    mainnet: quien lo despliega puede emitir tokens sin limite.
 *
 * Existe porque en BSC testnet no hay un USDC canonico. Desplegando este
 * contrato tenes una address concreta para poner en EVM_USDC_CONTRACT y podes
 * emitirte los tokens que necesites para probar.
 *
 * Decimales: 18 (lo habitual en BSC). El USDC real de Ethereum usa 6, por eso
 * EVM_TOKEN_DECIMALS es configurable: tiene que coincidir con el token que uses.
 */
contract TestUSDC {
    string public name = "Test USD Coin";
    string public symbol = "USDC";
    uint8 public decimals = 18;
    uint256 public totalSupply;

    address public owner;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    modifier onlyOwner() {
        require(msg.sender == owner, "solo el owner");
        _;
    }

    /// Al desplegar, acredita 1.000.000 de tokens a quien lo despliega.
    constructor() {
        owner = msg.sender;
        _mint(msg.sender, 1_000_000 * 10 ** uint256(decimals));
    }

    /// Emite tokens nuevos. Sirve para fondear wallets de prueba.
    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 permitido = allowance[from][msg.sender];
        require(permitido >= amount, "allowance insuficiente");
        if (permitido != type(uint256).max) {
            allowance[from][msg.sender] = permitido - amount;
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(to != address(0), "destino invalido");
        require(balanceOf[from] >= amount, "saldo insuficiente");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        // Este es el evento que lee el bridge para verificar los pagos.
        emit Transfer(from, to, amount);
    }

    function _mint(address to, uint256 amount) internal {
        require(to != address(0), "destino invalido");
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }
}
